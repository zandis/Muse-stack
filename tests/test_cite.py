"""Citation lookup with recorded responses. Nothing in this file opens a socket.

The PubMed and Europe PMC documents are the public bibliographic record of
Polack et al., N Engl J Med 2020 (PMID 33301246, DOI 10.1056/NEJMoa2034577),
trimmed to the fields the parser reads. The TFDA and NHI rows are synthetic.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from wardmuse.cite import http
from wardmuse.cite.drugs import (
    NHI_DATASET,
    NHI_ENDPOINT,
    TFDA_CANCELLED_DATASET,
    TFDA_CANCELLED_ENDPOINT,
    TFDA_DATASET,
    TFDA_ENDPOINT,
    lookup_nhi,
    lookup_tfda,
    parse_nhi_csv,
    roc_date,
    search_nhi,
)
from wardmuse.cite.europepmc import lookup_europepmc
from wardmuse.cite.http import CiteError, Pace, default_get
from wardmuse.cite.pubmed import lookup_pubmed
from wardmuse.cli import main

PMID = "33301246"
DOI = "10.1056/NEJMoa2034577"
TITLE = "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine."
TODAY = date(2026, 11, 17)
ADDRESS = "999 Nowhere Road, Example City"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cite tests must not use the network")

    monkeypatch.setattr(http.urllib.request, "urlopen", boom)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _response(by_path: dict[str, tuple[int, bytes]]):
    seen: list[str] = []

    def fetch(url: str, timeout: float) -> tuple[int, bytes]:
        seen.append(url)
        path = urlsplit(url).path
        for suffix, payload in by_path.items():
            if path.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected URL path {path}")

    return fetch, seen


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


ESEARCH = json.dumps({"esearchresult": {"count": "1", "retmax": "1", "idlist": [PMID]}}).encode()
ESUMMARY = json.dumps(
    {
        "result": {
            "uids": [PMID],
            PMID: {
                "uid": PMID,
                "pubdate": "2020 Dec 31",
                "source": "N Engl J Med",
                "fulljournalname": "The New England journal of medicine",
                "title": TITLE,
                "articleids": [
                    {"idtype": "pubmed", "value": PMID},
                    {"idtype": "doi", "value": "10.0000/from-summary"},
                ],
            },
        }
    }
).encode()
EFETCH = f"""<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">{PMID}</PMID>
      <Article>
        <Journal>
          <Title>The New England journal of medicine</Title>
          <ISOAbbreviation>N Engl J Med</ISOAbbreviation>
          <JournalIssue><PubDate><Year>2020</Year><Month>Dec</Month></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>{TITLE}</ArticleTitle>
        <ELocationID EIdType="pii">NEJMoa2034577</ELocationID>
        <ELocationID EIdType="doi" ValidYN="Y">{DOI}</ELocationID>
      </Article>
      <CommentsCorrectionsList><CommentsCorrections><PMID>33301245</PMID></CommentsCorrections></CommentsCorrectionsList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">{PMID}</ArticleId>
        <ArticleId IdType="doi">{DOI}</ArticleId>
      </ArticleIdList>
      <ReferenceList><Reference><ArticleIdList><ArticleId IdType="doi">10.1000/not-this-paper</ArticleId></ArticleIdList></Reference></ReferenceList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
""".encode()
EPMC = json.dumps(
    {
        "hitCount": 1,
        "resultList": {
            "result": [
                {
                    "id": PMID,
                    "source": "MED",
                    "pmid": PMID,
                    "doi": "10.1056/nejmoa2034577",
                    "title": f"<i>{TITLE}</i>",
                    "journalTitle": "N Engl J Med",
                    "pubYear": "2020",
                    "extra": "ignored",
                }
            ]
        },
    }
).encode()


def test_pubmed_sends_tool_and_email_uses_all_three_calls_and_stays_under_3_per_second():
    fetch, seen = _response({"/esearch.fcgi": (200, ESEARCH), "/esummary.fcgi": (200, ESUMMARY), "/efetch.fcgi": (200, EFETCH)})
    clock = Clock()
    hits = lookup_pubmed(DOI, fetch=fetch, environ={}, pace=Pace(sleep=clock.sleep, clock=clock.clock), note=lambda _text: None)
    assert [urlsplit(url).path.rsplit("/", 1)[-1] for url in seen] == ["esearch.fcgi", "esummary.fcgi", "efetch.fcgi"]
    for url in seen:
        params = _params(url)
        assert params["tool"] == ["wardmuse"]
        assert params["email"] == [http.DEFAULT_EMAIL]
        assert "api_key" not in params
        assert urlsplit(url).hostname == "eutils.ncbi.nlm.nih.gov"
    assert clock.sleeps == pytest.approx([1 / 3, 1 / 3])
    assert hits == [
        {
            "source": "pubmed",
            "pmid": PMID,
            "doi": DOI,  # efetch, not the summary's DOI, and not a reference DOI
            "title": TITLE,
            "journal": "The New England journal of medicine",
            "year": "2020",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/",
        }
    ]


def test_pubmed_sends_an_api_key_only_when_the_variable_is_present_and_never_repeats_it():
    key = "NCBItestKEY123"
    fetch, seen = _response({"/esearch.fcgi": (500, b"nope")})

    def run(env: dict[str, str]) -> CiteError:
        with pytest.raises(CiteError) as caught:
            lookup_pubmed("aspirin", fetch=fetch, environ=env, pace=Pace(sleep=lambda _s: None, clock=lambda: 0.0))
        return caught.value

    missing = run({})
    assert "api_key" not in seen[-1] and key not in missing.message
    present = run({"NCBI_API_KEY": key, "NCBI_EMAIL": "cite@example.com"})
    assert _params(seen[-1])["api_key"] == [key]
    assert _params(seen[-1])["email"] == ["cite@example.com"]
    assert key not in present.message and "nope" not in present.message
    assert present.code == "unavailable"


def test_pubmed_with_no_ids_does_not_ask_for_summaries():
    fetch, seen = _response({"/esearch.fcgi": (200, b'{"esearchresult":{"count":"0","idlist":[]}}')})
    assert lookup_pubmed("zzzz-not-a-real-topic-9f3a", fetch=fetch, environ={}, pace=Pace(sleep=lambda _s: None, clock=lambda: 0.0)) == []
    assert len(seen) == 1


def test_europepmc_recorded_search():
    fetch, seen = _response({"/search": (200, EPMC)})
    hits = lookup_europepmc(f"PMID {PMID} vaccine", fetch=fetch)
    assert urlsplit(seen[0]).hostname == "www.ebi.ac.uk"
    assert hits == [
        {
            "source": "europepmc",
            "pmid": PMID,
            "doi": "10.1056/nejmoa2034577",
            "title": TITLE,
            "journal": "N Engl J Med",
            "year": "2020",
            "url": f"https://europepmc.org/article/MED/{PMID}",
        }
    ]
    empty, _seen = _response({"/search": (200, b'{"hitCount": 0}')})
    assert lookup_europepmc("nothing-matches-this", fetch=empty) == []


def _zip(name: str, rows: list[dict[str, object]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, json.dumps(rows, ensure_ascii=False))
    return buffer.getvalue()


def _tfda_rows() -> list[dict[str, object]]:
    return [
        {
            "許可證字號": "測署藥製字第TEST0001號",
            "註銷狀態": "",
            "有效日期": "2030/01/01",
            "發證日期": "2020/01/02",
            "中文品名": "範例西林錠",
            "英文品名": "Examplecillin tablets 250 mg",
            "主成分略述": "EXAMPLECILLIN",
            "劑型": "錠劑",
            "申請商名稱": "Example Pharma",
            "申請商地址": ADDRESS,
            "申請商統一編號": "00000000",
        },
        {
            "許可證字號": "測署藥製字第TEST0002號",
            "註銷狀態": "",
            "有效日期": "2020/01/01",
            "發證日期": "2010/01/01",
            "中文品名": "別的藥",
            "英文品名": "Other tablets",
            "主成分略述": "OTHER",
            "劑型": "錠劑",
            "申請商名稱": "Other Co",
            "申請商地址": ADDRESS,
        },
    ]


def _tfda_zip() -> bytes:
    return _zip("37.json", _tfda_rows())


def _tfda_cancelled_rows() -> list[dict[str, object]]:
    return [
        {
            "許可證字號": "測署藥製字第TEST0009號",
            "註銷狀態": "已註銷",
            "註銷日期": "2018/01/17",
            "註銷理由": "自請註銷",
            "有效日期": "2031/01/01",
            "發證日期": "2009/07/27",
            "中文品名": "舊範例西林錠",
            "英文品名": "Old Examplecillin tablets",
            "主成分略述": "EXAMPLECILLIN",
            "劑型": "錠劑",
            "申請商名稱": "Former Pharma",
            "申請商地址": ADDRESS,
            "申請商統一編號": "11111111",
        },
        {
            "許可證字號": "測署藥製字第TEST0008號",
            "註銷狀態": "已廢止",
            "註銷日期": "2015/06/01",
            "有效日期": "2016/06/01",
            "發證日期": "2005/06/01",
            "中文品名": "廢止藥",
            "英文品名": "Abolished tablets",
            "主成分略述": "ABOLISHINE",
            "劑型": "錠劑",
            "申請商名稱": "Gone Co",
            "申請商地址": ADDRESS,
        },
        {
            "許可證字號": "測署藥製字第TEST0099號",
            "註銷狀態": "",
            "有效日期": "2030/01/01",
            "發證日期": "2020/01/02",
            "中文品名": "僅在全集",
            "英文品名": "Only In The Full File",
            "主成分略述": "GHOSTCILLIN",
            "劑型": "錠劑",
            "申請商名稱": "Ghost Pharma",
            "申請商地址": ADDRESS,
        },
        {
            "許可證字號": "測署藥製字第TEST0001號",
            "註銷狀態": "已註銷",
            "有效日期": "2019/01/01",
            "發證日期": "2000/01/01",
            "中文品名": "範例西林錠",
            "英文品名": "Examplecillin tablets 250 mg",
            "主成分略述": "EXAMPLECILLIN",
            "劑型": "錠劑",
            "申請商名稱": "Example Pharma",
            "申請商地址": ADDRESS,
        },
    ]


def _tfda_cancelled_zip() -> bytes:
    return _zip("36.json", _tfda_cancelled_rows())


def _tfda_fetch(active: bytes, cancelled: bytes):
    seen: list[str] = []

    def fetch(url: str, timeout: float) -> tuple[int, bytes]:
        seen.append(url)
        if url == TFDA_ENDPOINT:
            return 200, active
        if url == TFDA_CANCELLED_ENDPOINT:
            return 200, cancelled
        raise AssertionError(url)

    return fetch, seen


def test_tfda_licence_from_a_zip_fixture_omits_addresses():
    active = _tfda_zip()
    cancelled = _tfda_cancelled_zip()
    fetch, seen = _tfda_fetch(active, cancelled)
    hits = lookup_tfda("examplecillin", fetch=fetch, today=TODAY)
    assert seen == [TFDA_ENDPOINT, TFDA_CANCELLED_ENDPOINT]
    assert [hit["license_number"] for hit in hits] == ["測署藥製字第TEST0001號", "測署藥製字第TEST0009號"]
    hit = hits[0]
    assert hit["name_en"] == "Examplecillin tablets 250 mg"
    assert hit["name_zh"] == "範例西林錠"
    assert hit["ingredient"] == "EXAMPLECILLIN"
    assert hit["issued"] == "2020-01-02"
    assert hit["valid_until"] == "2030-01-01"
    assert hit["status"] == "active"
    assert hit["url"] == TFDA_DATASET
    assert hit["endpoint"] == TFDA_ENDPOINT
    assert ADDRESS not in json.dumps(hit, ensure_ascii=False)
    assert "00000000" not in json.dumps(hit)
    old = hits[1]
    assert old["status"] == "cancelled"
    assert old["name_en"] == "Old Examplecillin tablets"
    assert old["valid_until"] == "2031-01-01"
    assert old["url"] == TFDA_CANCELLED_DATASET
    assert old["endpoint"] == TFDA_CANCELLED_ENDPOINT
    assert ADDRESS not in json.dumps(old, ensure_ascii=False)
    assert "11111111" not in json.dumps(old)
    assert "自請註銷" not in json.dumps(old, ensure_ascii=False)
    expired = lookup_tfda("測署藥製字第TEST0002號", fetch=fetch, today=TODAY)
    assert expired[0]["status"] == "逾期"
    assert expired[0]["endpoint"] == TFDA_ENDPOINT
    abolished = lookup_tfda("測署藥製字第TEST0008號", fetch=fetch, today=TODAY)
    assert abolished[0]["status"] == "cancelled"
    assert abolished[0]["endpoint"] == TFDA_CANCELLED_ENDPOINT
    assert lookup_tfda("ghostcillin", fetch=fetch, today=TODAY) == []


def test_tfda_caches_the_active_and_cancelled_files_for_seven_days(tmp_path: Path):
    fetch, seen = _tfda_fetch(_tfda_zip(), _tfda_cancelled_zip())
    first = lookup_tfda("examplecillin", fetch=fetch, cache_dir=tmp_path, today=TODAY)
    second = lookup_tfda("examplecillin", fetch=fetch, cache_dir=tmp_path, today=TODAY)
    assert seen == [TFDA_ENDPOINT, TFDA_CANCELLED_ENDPOINT]
    assert [hit["status"] for hit in first] == ["active", "cancelled"]
    assert second == first
    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ["tfda-36.json.zip", "tfda-37.json.zip"]
    cancelled = tmp_path / "tfda-36.json.zip"
    os_utime(cancelled, cancelled.stat().st_mtime - (8 * 24 * 3600))
    lookup_tfda("examplecillin", fetch=fetch, cache_dir=tmp_path, today=TODAY)
    assert seen == [TFDA_ENDPOINT, TFDA_CANCELLED_ENDPOINT, TFDA_CANCELLED_ENDPOINT]
    active = tmp_path / "tfda-37.json.zip"
    os_utime(active, active.stat().st_mtime - (8 * 24 * 3600))
    lookup_tfda("examplecillin", fetch=fetch, cache_dir=tmp_path, today=TODAY)
    assert seen == [TFDA_ENDPOINT, TFDA_CANCELLED_ENDPOINT, TFDA_CANCELLED_ENDPOINT, TFDA_ENDPOINT]


NHI_CSV = (
    "\ufeff異動,藥品代號,藥品英文名稱,藥品中文名稱,成分,規格量,規格單位,單複方,支付價,有效起日,有效迄日,藥商,製造廠名稱,劑型\n"
    ",WEAK0001,Examplecillin cream,弱匹配,EXAMPLECILLIN,1,mg,單方,1.00,1000101,1101231,Weak Co,,乳膏\n"
    ",EX0001,Old Examplecillin,舊範例西林,EXAMPLECILLIN,250,mg,單方,1.00,1000101,1101231,Example Pharma,,錠劑\n"
    ",EX0001,Future Examplecillin,未來範例西林,EXAMPLECILLIN,250,mg,單方,8.00,1200101,9991231,Example Pharma,,錠劑\n"
    ",EX0001,Examplecillin tablets,範例西林錠,EXAMPLECILLIN,250,mg,單方,9.50,1120101,9991231,Example Pharma,,錠劑\n"
    ',EX0002,"Quoted, name",引號藥,OTHER,,,單方,3.00,1120101,9991231,Quote Co,,錠劑\n'
).encode()


def test_nhi_keeps_the_price_in_force_and_an_exact_code():
    assert roc_date("  960901") == date(2007, 9, 1)
    assert roc_date("1150101") == date(2026, 1, 1)
    assert roc_date("9991231") is None
    rows = parse_nhi_csv(NHI_CSV)
    hits = search_nhi(rows, "EX0001", 5, TODAY)
    assert [hit["drug_code"] for hit in hits] == ["EX0001"]
    assert hits[0]["price"] == "9.50"
    assert hits[0]["price_from"] == "2023-01-01"
    assert hits[0]["price_until"] is None
    assert hits[0]["url"] == NHI_DATASET and hits[0]["endpoint"] == NHI_ENDPOINT
    # A weak earlier name match must not crowd out the exact code when only one hit is kept.
    assert search_nhi(rows, "EX0001", 1, TODAY)[0]["drug_code"] == "EX0001"
    quoted = search_nhi(rows, "Quoted, name", 5, TODAY)
    assert quoted[0]["name_en"] == "Quoted, name" and quoted[0]["price"] == "3.00"


def test_nhi_lookup_reads_the_published_endpoint_and_caches_it(tmp_path: Path):
    calls = {"n": 0}

    def fetch(url: str, timeout: float) -> tuple[int, bytes]:
        calls["n"] += 1
        assert url == NHI_ENDPOINT
        return 200, NHI_CSV

    first = lookup_nhi("範例西林錠", fetch=fetch, cache_dir=tmp_path, today=TODAY, limit=1)
    second = lookup_nhi("範例西林錠", fetch=fetch, cache_dir=tmp_path, today=TODAY, limit=1)
    assert calls["n"] == 1
    assert first[0]["drug_code"] == "EX0001" and first[0]["price"] == "9.50" and second == first
    cached = next(tmp_path.iterdir())
    stale = cached.stat().st_mtime - (8 * 24 * 3600)
    os_utime(cached, stale)
    lookup_nhi("範例西林錠", fetch=fetch, cache_dir=tmp_path, today=TODAY, limit=1)
    assert calls["n"] == 2


def os_utime(path: Path, when: float) -> None:
    import os

    os.utime(path, (when, when))


def test_queries_that_must_not_be_sent():
    with pytest.raises(CiteError) as empty:
        lookup_europepmc("   ", fetch=lambda *_a: (_ for _ in ()).throw(AssertionError("sent")))
    assert empty.value.code == "invalid_input"
    secret = "sk-" + "aB3" * 8
    with pytest.raises(CiteError) as refused:
        lookup_pubmed(secret, fetch=lambda *_a: (_ for _ in ()).throw(AssertionError("sent")), environ={})
    assert refused.value.code == "refused" and secret not in refused.value.message and "sk-" not in refused.value.message


def test_redirects_and_other_hosts_are_refused_before_a_connection():
    opened: list[str] = []

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> None:
            opened.append("opened")
            raise AssertionError("opened")

    with pytest.raises(CiteError) as foreign:
        default_get("https://example.com/entrez", 1, opener=Opener())
    with pytest.raises(CiteError):
        default_get("http://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", 1, opener=Opener())
    assert foreign.value.code == "refused" and opened == []
    handler = http._NoRedirect()
    with pytest.raises(CiteError) as redirected:
        handler.redirect_request(None, io.BytesIO(b"go"), 302, "Found", {}, "https://example.com/")
    assert redirected.value.code == "redirect"


def test_pace_cannot_be_set_faster_than_3_per_second():
    with pytest.raises(ValueError):
        Pace(per_second=10)


def test_cli_prints_json_and_does_not_download(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    key = "NCBItestKEY123"
    monkeypatch.setenv("NCBI_API_KEY", key)
    seen: dict[str, str] = {}

    def fake_pubmed(query: str, **_kwargs: object) -> list[dict[str, object]]:
        seen["query"] = query
        return [
            {
                "source": "pubmed",
                "pmid": PMID,
                "doi": DOI,
                "title": TITLE,
                "journal": "N Engl J Med",
                "year": "2020",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{PMID}/",
            }
        ]

    monkeypatch.setattr("wardmuse.cite.cli.lookup_pubmed", fake_pubmed)
    assert main(["cite", "pubmed", DOI, "--json"]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["results"][0]["pmid"] == PMID
    assert payload["results"][0]["url"].endswith(f"/{PMID}/")
    assert key not in out.out and key not in out.err
    assert seen["query"] == DOI
    assert main(["cite"]) == 2
    assert main(["cite", "pubmed", "aspirin", "--limit", "0"]) == 2
