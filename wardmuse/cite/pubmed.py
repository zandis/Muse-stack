"""PubMed citations through NCBI E-utilities.

Three calls, in order, for one lookup: ``esearch``, ``esummary``, ``efetch``.
Each carries ``tool`` and ``email``. ``api_key`` is added only when
``NCBI_API_KEY`` is present. The three calls share one pace of at most 3 per
second. Checked against the E-utilities book (esearch, esummary, efetch, JSON
and XML) on 2026-11-17.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from wardmuse.cite.http import Pace, call, clean_query, default_get, ncbi_params
from wardmuse.cite.http import Fetch

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH = f"{EUTILS}/esearch.fcgi"
ESUMMARY = f"{EUTILS}/esummary.fcgi"
EFETCH = f"{EUTILS}/efetch.fcgi"
PUBMED_ARTICLE = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

_PMID = re.compile(r"[1-9][0-9]{0,8}\Z")
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\Z")
_YEAR = re.compile(r"\b(1[89][0-9]{2}|20[0-9]{2})\b")

Note = Callable[[str], None]


def _term(query: str) -> str:
    if _PMID.fullmatch(query):
        return f"{query}[uid]"
    if _DOI.fullmatch(query):
        return f"{query}[doi]"
    return query


def _year_in(text: str) -> str | None:
    match = _YEAR.search(text or "")
    return match.group(1) if match else None


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for kid in element:
        if _tag(kid) == name:
            return kid
    return None


def _path(element: ET.Element | None, names: list[str]) -> ET.Element | None:
    node = element
    for name in names:
        node = _child(node, name)
        if node is None:
            return None
    return node


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _doi_ok(value: str) -> str | None:
    text = (value or "").strip()
    return text if _DOI.fullmatch(text) else None


def _pmid_ok(value: str) -> str | None:
    text = (value or "").strip()
    return text if _PMID.fullmatch(text) else None


def article_url(pmid: str | None, doi: str | None) -> str | None:
    if pmid:
        return PUBMED_ARTICLE.format(pmid=pmid)
    if doi:
        return "https://doi.org/" + doi
    return None


def parse_esearch(payload: bytes) -> list[str]:
    """PMIDs from an esearch JSON document, in the order NCBI returned them."""

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CiteParse("PubMed esearch did not return the expected document.") from None
    result = data.get("esearchresult") if isinstance(data, dict) else None
    idlist = result.get("idlist") if isinstance(result, dict) else None
    if not isinstance(idlist, list):
        raise CiteParse("PubMed esearch did not return the expected document.")
    found: list[str] = []
    for item in idlist:
        pmid = _pmid_ok(str(item))
        if pmid and pmid not in found:
            found.append(pmid)
    return found


class CiteParse(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_esummary(payload: bytes) -> dict[str, dict[str, Any]]:
    """``{pmid: fields}`` from an esummary JSON document. A uid NCBI could not summarise is skipped."""

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CiteParse("PubMed esummary did not return the expected document.") from None
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise CiteParse("PubMed esummary did not return the expected document.")
    uids = result.get("uids")
    if not isinstance(uids, list):
        uids = [key for key in result if key != "uids"]
    out: dict[str, dict[str, Any]] = {}
    for uid in uids:
        pmid = _pmid_ok(str(uid))
        row = result.get(str(uid))
        if not pmid or not isinstance(row, dict) or row.get("error"):
            continue
        doi = None
        for item in row.get("articleids") or []:
            if isinstance(item, dict) and str(item.get("idtype") or "").lower() == "doi":
                doi = _doi_ok(str(item.get("value") or ""))
                if doi:
                    break
        journal = " ".join(str(row.get("fulljournalname") or row.get("source") or "").split())
        title = " ".join(str(row.get("title") or "").split())
        year = _year_in(str(row.get("pubdate") or "")) or _year_in(str(row.get("sortpubdate") or ""))
        out[pmid] = {"pmid": pmid, "doi": doi, "title": title, "journal": journal, "year": year}
    return out


def _one_article(article: ET.Element) -> dict[str, Any] | None:
    pmid = _pmid_ok(_text(_path(article, ["MedlineCitation", "PMID"])))
    title = _text(_path(article, ["MedlineCitation", "Article", "ArticleTitle"]))
    journal = _text(_path(article, ["MedlineCitation", "Article", "Journal", "Title"]))
    if not journal:
        journal = _text(_path(article, ["MedlineCitation", "Article", "Journal", "ISOAbbreviation"]))
    pubdate = _path(article, ["MedlineCitation", "Article", "Journal", "JournalIssue", "PubDate"])
    year = _text(_child(pubdate, "Year")) if pubdate is not None else ""
    if not _YEAR.fullmatch(year or ""):
        year = _year_in(_text(_child(pubdate, "MedlineDate")) if pubdate is not None else "") or ""
    doi = None
    article_node = _path(article, ["MedlineCitation", "Article"])
    if article_node is not None:
        for node in article_node:
            if _tag(node) == "ELocationID" and str(node.attrib.get("EIdType") or "").lower() == "doi":
                doi = _doi_ok(_text(node))
                if doi:
                    break
    if not doi:
        id_list = _path(article, ["PubmedData", "ArticleIdList"])
        if id_list is not None:
            for node in id_list:
                if _tag(node) == "ArticleId" and str(node.attrib.get("IdType") or "").lower() == "doi":
                    doi = _doi_ok(_text(node))
                    if doi:
                        break
    if not pmid and not doi:
        return None
    return {"pmid": pmid, "doi": doi, "title": title, "journal": journal, "year": year or None}


def parse_efetch(payload: bytes) -> dict[str, dict[str, Any]]:
    """``{pmid: fields}`` from an efetch XML document. Reference DOIs are not read."""

    try:
        root = ET.fromstring(payload.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError, ValueError):
        raise CiteParse("PubMed efetch did not return the expected document.") from None
    if _tag(root) != "PubmedArticleSet":
        raise CiteParse("PubMed efetch did not return the expected document.")
    out: dict[str, dict[str, Any]] = {}
    for node in root:
        if _tag(node) != "PubmedArticle":
            continue
        row = _one_article(node)
        if row and row.get("pmid"):
            out[str(row["pmid"])] = row
    return out


def _merge(pmids: list[str], summary: dict[str, dict[str, Any]], fetched: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """efetch wins for a field it actually has. esearch order is kept."""

    citations: list[dict[str, Any]] = []
    for pmid in pmids:
        base = dict(summary.get(pmid) or {"pmid": pmid, "doi": None, "title": "", "journal": "", "year": None})
        full = fetched.get(pmid)
        if full:
            for key in ("doi", "title", "journal", "year"):
                if full.get(key):
                    base[key] = full[key]
            base["pmid"] = full.get("pmid") or pmid
        title = " ".join(str(base.get("title") or "").split())
        journal = " ".join(str(base.get("journal") or "").split())
        doi = _doi_ok(str(base.get("doi") or "")) if base.get("doi") else None
        year = base.get("year") if isinstance(base.get("year"), str) and _YEAR.fullmatch(str(base.get("year"))) else None
        if not title and not doi:
            continue
        citations.append(
            {
                "source": "pubmed",
                "pmid": pmid,
                "doi": doi,
                "title": title,
                "journal": journal,
                "year": year,
                "url": article_url(pmid, doi),
            }
        )
    return citations


def _url(base: str, params: Mapping[str, str]) -> str:
    return base + "?" + urlencode(params)


def lookup_pubmed(
    query: str,
    *,
    limit: int = 5,
    fetch: Fetch | None = None,
    environ: Mapping[str, str] | None = None,
    pace: Pace | None = None,
    timeout: float = 20.0,
    note: Note | None = None,
) -> list[dict[str, Any]]:
    """Citations for ``query`` (a PMID, a DOI, or PubMed words).

    ``fetch(url, timeout) -> (status, body)`` replaces the network in tests.
    ``environ`` is where ``NCBI_API_KEY`` and ``NCBI_EMAIL`` are read; pass a
    mapping so the process environment is not used. ``note`` is accepted so the
    command can pass one callback to every source; a citation search does not use it.
    """

    from wardmuse.cite.http import CiteError

    text = clean_query(query, limit=limit)
    env = os.environ if environ is None else environ
    identity = ncbi_params(env)
    _ = note
    getter = fetch or (lambda url, timeout_: default_get(url, timeout_, max_bytes=2_000_000))
    clock = pace or Pace()

    def paced(url: str) -> bytes:
        clock.wait()
        return call(getter, url, timeout, what="PubMed")

    try:
        id_bytes = paced(_url(ESEARCH, {"db": "pubmed", "term": _term(text), "retmode": "json", "retmax": str(limit), **identity}))
        pmids = parse_esearch(id_bytes)[:limit]
    except CiteParse as exc:
        raise CiteError("bad_response", exc.message) from None
    if not pmids:
        return []
    ids = ",".join(pmids)
    try:
        summary = parse_esummary(paced(_url(ESUMMARY, {"db": "pubmed", "id": ids, "retmode": "json", **identity})))
        fetched = parse_efetch(paced(_url(EFETCH, {"db": "pubmed", "id": ids, "retmode": "xml", **identity})))
    except CiteParse as exc:
        raise CiteError("bad_response", exc.message) from None
    return _merge(pmids, summary, fetched)
