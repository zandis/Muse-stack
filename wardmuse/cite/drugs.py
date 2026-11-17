"""Taiwan drug licence and NHI price lookup from public open data.

The sources are bulk files, not a per-name query API. The addresses were read
from data.gov.tw on 2026-11-17:

* TFDA non-cancelled drug licences, dataset 9123, resource id 37. The file is a
  ZIP of one JSON array. Download:
  ``https://data.fda.gov.tw/data/opendata/export/37/json``
* TFDA full licence file, dataset 9122, resource id 36. The same ZIP-of-JSON
  shape. Rows whose 註銷狀態 is set (已註銷 or 已廢止) are the cancelled
  licences; a blank 註銷狀態 is already in dataset 9123. Download:
  ``https://data.fda.gov.tw/data/opendata/export/36/json``
* NHI drug items and prices, dataset 23715, resource id
  ``A21030000I-E41001-001``. The file is a UTF-8 CSV of about 96 MB.
  Download:
  ``https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001``

A copy of each file is kept under the Wardmuse home for 7 days. Results cite
the data.gov.tw dataset page and name the endpoint that supplied the bytes.
Company addresses and business numbers are not copied out of the file. This is
labelling and reimbursement data, not a reason to prescribe.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from wardmuse.cite.http import Fetch, call, clean_query, default_get

TFDA_ENDPOINT = "https://data.fda.gov.tw/data/opendata/export/37/json"
TFDA_DATASET = "https://data.gov.tw/dataset/9123"
# Dataset 9122 is every licence. The lookup keeps the rows with 註銷狀態 set.
TFDA_CANCELLED_ENDPOINT = "https://data.fda.gov.tw/data/opendata/export/36/json"
TFDA_CANCELLED_DATASET = "https://data.gov.tw/dataset/9122"
NHI_ENDPOINT = "https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001"
NHI_DATASET = "https://data.gov.tw/dataset/23715"

TFDA_MAX_BYTES = 30_000_000  # dataset 9123 zip was 4.3 MB on 2026-11-17
TFDA_CANCELLED_MAX_BYTES = 40_000_000  # dataset 9122 zip was 11.9 MB on 2026-11-17
TFDA_MAX_UNCOMPRESSED = 200_000_000  # 9122 JSON was 79 MB; 9123 JSON was 29 MB
NHI_MAX_BYTES = 200_000_000  # the CSV was 96 MB on 2026-11-17
CACHE_MAX_AGE_S = 7 * 24 * 3600
_FIELD_CAP = 300
_STATE = "_tfda_state"
_DATASET = "_tfda_dataset"
_ENDPOINT_KEY = "_tfda_endpoint"

Note = Callable[[str], None]

_GREGORIAN = re.compile(r"(\d{4})[/-](\d{2})[/-](\d{2})")


def _fold(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _clip(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= _FIELD_CAP:
        return text
    return text[: _FIELD_CAP - 1] + "…"


def _gregorian(value: Any) -> str | None:
    match = _GREGORIAN.fullmatch(" ".join(str(value or "").split()))
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _roc_parts(value: Any) -> tuple[int, int, int] | None:
    """``YYYMMDD`` or ``YYMMDD`` as the NHI file writes a Republic of China date."""

    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 7:
        year, month, day = int(digits[:3]), int(digits[3:5]), int(digits[5:])
    elif len(digits) == 6:
        year, month, day = int(digits[:2]), int(digits[2:4]), int(digits[4:])
    else:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def roc_date(value: Any) -> date | None:
    """A Republic of China date as a Gregorian ``date``. Year 999 (CE 2910) is the open-ended sentinel, not a day."""

    parts = _roc_parts(value)
    if not parts or parts[0] >= 999:
        return None
    try:
        return date(parts[0] + 1911, parts[1], parts[2])
    except ValueError:
        return None


def _open_end(value: Any) -> bool:
    parts = _roc_parts(value)
    return bool(parts and parts[0] >= 999)


def _covers(row: Mapping[str, Any], today: date) -> bool:
    start = roc_date(row.get("有效起日"))
    if start is None or start > today:
        return False
    if _open_end(row.get("有效迄日")):
        return True
    end = roc_date(row.get("有效迄日"))
    return bool(end and end >= today)


def _price_key(row: Mapping[str, Any], today: date) -> tuple[int, int, date]:
    """Covering ``today`` wins, then a price that has already started, then the later start."""

    start = roc_date(row.get("有效起日")) or date.min
    return (1 if _covers(row, today) else 0, 1 if start <= today else 0, start)


def _prefer(current: Mapping[str, Any], new: Mapping[str, Any], today: date) -> Mapping[str, Any]:
    return new if _price_key(new, today) >= _price_key(current, today) else current


def _cache_path(cache_dir: Path | None, name: str, *, use_home: bool) -> Path | None:
    if cache_dir is None and not use_home:
        return None
    directory = cache_dir
    if directory is None:
        from wardmuse.paths import home

        directory = home() / "cite-cache"
    return directory / name


def load_cached(
    url: str,
    path: Path | None,
    fetch: Fetch,
    *,
    max_age_s: float,
    max_bytes: int,
    timeout: float,
    note: Note | None,
    label: str,
    what: str,
) -> bytes:
    """Bytes of ``url``, from ``path`` when that file is still young enough."""

    now = time.time()
    if path is not None and path.is_file():
        age = now - path.stat().st_mtime
        size = path.stat().st_size
        if 0 <= age <= max_age_s and 0 < size <= max_bytes:
            return path.read_bytes()
    if note:
        note(label)
    body = call(fetch, url, timeout, what=what)
    if len(body) > max_bytes:
        from wardmuse.cite.http import CiteError

        raise CiteError("too_large", "The public data file is larger than this tool will read.")
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        temporary.write_bytes(body)
        os.replace(temporary, path)
    return body


def parse_tfda(payload: bytes) -> list[dict[str, Any]]:
    """Records from the TFDA JSON file, or from the ZIP that file is published as."""

    from wardmuse.cite.http import CiteError

    raw = payload
    if payload[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile:
            raise CiteError("bad_response", "The TFDA file was not a readable archive.") from None
        chosen = None
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if info.is_dir() or not info.filename.lower().endswith(".json"):
                continue
            if info.filename.startswith("/") or ".." in parts:
                raise CiteError("bad_response", "The TFDA file was not a readable archive.")
            if info.file_size > TFDA_MAX_UNCOMPRESSED:
                raise CiteError("too_large", "The TFDA file is larger than this tool will read.")
            chosen = info
            break
        if chosen is None:
            raise CiteError("bad_response", "The TFDA file was not a readable archive.")
        raw = archive.read(chosen)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CiteError("bad_response", "The TFDA file was not the expected list of licences.") from None
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        raise CiteError("bad_response", "The TFDA file was not the expected list of licences.")
    return [row for row in data if isinstance(row, dict)]


def _tfda_rank(query: str, row: Mapping[str, Any]) -> int | None:
    folded = _fold(query)
    if not folded:
        return None
    licence = _fold(row.get("許可證字號"))
    english = _fold(row.get("英文品名"))
    chinese = _fold(row.get("中文品名"))
    ingredient = _fold(row.get("主成分略述"))
    if folded == licence:
        return 0
    if folded and folded in licence:
        return 1
    if folded in {english, chinese, ingredient}:
        return 2
    if english.startswith(folded) or chinese.startswith(folded):
        return 3
    if folded in english or folded in chinese or folded in ingredient:
        return 4
    return None


def _cancelled_mark(row: Mapping[str, Any]) -> str:
    """The 註銷狀態 text. Blank means the licence is still in the non-cancelled file."""

    return _clip(row.get("註銷狀態"))


def _tag_tfda(
    rows: Iterable[Mapping[str, Any]], *, state: str, dataset: str, endpoint: str
) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item[_STATE] = state
        item[_DATASET] = dataset
        item[_ENDPOINT_KEY] = endpoint
        tagged.append(item)
    return tagged


def _tfda_status(row: Mapping[str, Any], today: date) -> str:
    """``cancelled`` for a dataset 9122 row, otherwise ``active`` or ``逾期``.

    A 註銷狀態 written into the non-cancelled file is kept as printed there.
    """

    if row.get(_STATE) == "cancelled":
        return "cancelled"
    marked = _cancelled_mark(row)
    if marked:
        return marked
    until = _gregorian(row.get("有效日期"))
    if until and date.fromisoformat(until) < today:
        return "逾期"
    return "active"


def search_tfda(rows: Iterable[Mapping[str, Any]], query: str, limit: int, today: date) -> list[dict[str, Any]]:
    """Up to ``limit`` licences matching ``query``, best match first.

    An active licence sorts ahead of a cancelled one of the same quality, and
    the same licence number is listed once. The active copy wins that tie.
    """

    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for row in rows:
        rank = _tfda_rank(query, row)
        if rank is None:
            continue
        licence = _clip(row.get("許可證字號"))
        origin = 1 if row.get(_STATE) == "cancelled" else 0
        ranked.append((rank, origin, licence, dict(row)))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, licence, row in ranked:
        if licence in seen:
            continue
        seen.add(licence)
        dataset = row.get(_DATASET) or TFDA_DATASET
        endpoint = row.get(_ENDPOINT_KEY) or TFDA_ENDPOINT
        hits.append(
            {
                "source": "tfda",
                "license_number": licence,
                "name_zh": _clip(row.get("中文品名")),
                "name_en": _clip(row.get("英文品名")),
                "ingredient": _clip(row.get("主成分略述")),
                "dosage_form": _clip(row.get("劑型")),
                "holder": _clip(row.get("申請商名稱")),
                "status": _tfda_status(row, today),
                "issued": _gregorian(row.get("發證日期")),
                "valid_until": _gregorian(row.get("有效日期")),
                "url": dataset,
                "endpoint": endpoint,
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _nhi_rank(query: str, row: Mapping[str, Any]) -> int | None:
    folded = _fold(query)
    if not folded:
        return None
    code = _fold(row.get("藥品代號"))
    english = _fold(row.get("藥品英文名稱"))
    chinese = _fold(row.get("藥品中文名稱"))
    ingredient = _fold(row.get("成分"))
    if folded == code:
        return 0
    if folded in {english, chinese, ingredient}:
        return 1
    if english.startswith(folded) or chinese.startswith(folded) or code.startswith(folded):
        return 2
    if folded in english or folded in chinese or folded in ingredient or folded in code:
        return 3
    return None


def _nhi_hit(row: Mapping[str, Any]) -> dict[str, Any]:
    until = None if _open_end(row.get("有效迄日")) else roc_date(row.get("有效迄日"))
    start = roc_date(row.get("有效起日"))
    return {
        "source": "nhi",
        "drug_code": _clip(row.get("藥品代號")),
        "name_zh": _clip(row.get("藥品中文名稱")),
        "name_en": _clip(row.get("藥品英文名稱")),
        "ingredient": _clip(row.get("成分")),
        "dosage_form": _clip(row.get("劑型")),
        "price": _clip(row.get("支付價")),
        "price_from": start.isoformat() if start else None,
        "price_until": until.isoformat() if until else None,
        "vendor": _clip(row.get("藥商")),
        "url": NHI_DATASET,
        "endpoint": NHI_ENDPOINT,
    }


def search_nhi(rows: Iterable[Mapping[str, Any]], query: str, limit: int, today: date) -> list[dict[str, Any]]:
    """Up to ``limit`` drugs. Each drug keeps the price row that covers ``today`` when there is one.

    The file is scanned to the end so a later exact match is not lost behind earlier weak ones.
    Only ``limit`` drugs are kept in memory.
    """

    kept: dict[str, tuple[int, int, Mapping[str, Any]]] = {}
    seen = 0
    for row in rows:
        rank = _nhi_rank(query, row)
        if rank is None:
            continue
        code = _fold(row.get("藥品代號")) or (_fold(row.get("藥品英文名稱")) + "|" + _fold(row.get("藥品中文名稱")))
        if not code.strip("|"):
            continue
        previous = kept.get(code)
        if previous is None:
            seen += 1
            if len(kept) >= limit:
                worst = max(kept, key=lambda key: (kept[key][0], kept[key][1]))
                if (rank, seen) >= (kept[worst][0], kept[worst][1]):
                    continue
                del kept[worst]
            kept[code] = (rank, seen, row)
        else:
            kept[code] = (min(rank, previous[0]), previous[1], _prefer(previous[2], row, today))
    ordered = sorted(kept, key=lambda key: (kept[key][0], kept[key][1]))
    return [_nhi_hit(kept[key][2]) for key in ordered]


def open_nhi_csv(payload: bytes) -> Iterable[dict[str, str]]:
    """Rows of the NHI CSV. The published file is UTF-8 with a BOM.

    The header is checked before this returns, so a wrong file fails before a
    search starts. Rows are then read one at a time.
    """

    from wardmuse.cite.http import CiteError

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CiteError("bad_response", "The NHI file was not the expected UTF-8 table.") from None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "藥品代號" not in reader.fieldnames or "支付價" not in reader.fieldnames:
        raise CiteError("bad_response", "The NHI file was not the expected table of drug prices.")

    def rows() -> Iterable[dict[str, str]]:
        for row in reader:
            if any((value or "").strip() for value in row.values()):
                yield {key: value for key, value in row.items() if key is not None}

    return rows()


def parse_nhi_csv(payload: bytes) -> list[dict[str, str]]:
    """Every row. :func:`open_nhi_csv` is what a lookup uses, so the live file is not held as a list."""

    return list(open_nhi_csv(payload))


def _finish(path: Path | None, body: bytes, parse: Callable[[bytes], Any]) -> Any:
    from wardmuse.cite.http import CiteError

    try:
        return parse(body)
    except CiteError:
        if path is not None and path.is_file():
            path.unlink(missing_ok=True)
        raise


def _download_cap(url: str) -> int:
    if url == TFDA_CANCELLED_ENDPOINT:
        return TFDA_CANCELLED_MAX_BYTES
    return TFDA_MAX_BYTES


def lookup_tfda(
    query: str,
    *,
    limit: int = 5,
    fetch: Fetch | None = None,
    cache_dir: Path | None = None,
    today: date | None = None,
    timeout: float = 180.0,
    note: Note | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[dict[str, Any]]:
    """Drug licences whose number, name or ingredient matches ``query``.

    Active licences come from dataset 9123 and are marked ``active`` (or ``逾期``
    once the validity date has passed). Cancelled and abolished licences come
    from dataset 9122, the full file, and are marked ``cancelled``. A row in
    that file with a blank 註銷狀態 is left to dataset 9123 so it is not listed
    twice. ``fetch`` replaces the downloads. Without it, each ZIP is cached for
    7 days. ``today`` decides 逾期; tests pass a fixed day.
    """

    text = clean_query(query, limit=limit)
    day = today or (clock() if clock else datetime.now()).date()
    use_home = fetch is None and cache_dir is None
    getter = fetch or (lambda url, timeout_: default_get(url, timeout_, max_bytes=_download_cap(url)))
    active_path = _cache_path(cache_dir, "tfda-37.json.zip", use_home=use_home)
    active_body = load_cached(
        TFDA_ENDPOINT,
        active_path,
        getter,
        max_age_s=CACHE_MAX_AGE_S,
        max_bytes=TFDA_MAX_BYTES,
        timeout=timeout,
        note=note,
        label="Downloading the TFDA drug-licence file (data.gov.tw dataset 9123). It is cached for 7 days.",
        what="TFDA",
    )
    active_rows = _finish(active_path, active_body, parse_tfda)
    cancelled_path = _cache_path(cache_dir, "tfda-36.json.zip", use_home=use_home)
    cancelled_body = load_cached(
        TFDA_CANCELLED_ENDPOINT,
        cancelled_path,
        getter,
        max_age_s=CACHE_MAX_AGE_S,
        max_bytes=TFDA_CANCELLED_MAX_BYTES,
        timeout=timeout,
        note=note,
        label="Downloading the TFDA licence file for cancelled licences (data.gov.tw dataset 9122). It is cached for 7 days.",
        what="TFDA",
    )
    cancelled_rows = _finish(cancelled_path, cancelled_body, parse_tfda)
    rows = _tag_tfda(active_rows, state="active", dataset=TFDA_DATASET, endpoint=TFDA_ENDPOINT)
    rows.extend(
        _tag_tfda(
            (row for row in cancelled_rows if _cancelled_mark(row)),
            state="cancelled",
            dataset=TFDA_CANCELLED_DATASET,
            endpoint=TFDA_CANCELLED_ENDPOINT,
        )
    )
    return search_tfda(rows, text, limit, day)


def lookup_nhi(
    query: str,
    *,
    limit: int = 5,
    fetch: Fetch | None = None,
    cache_dir: Path | None = None,
    today: date | None = None,
    timeout: float = 180.0,
    note: Note | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[dict[str, Any]]:
    """NHI prices whose code, name or ingredient matches ``query``.

    ``fetch`` replaces the download. Without it, the CSV is cached for 7 days.
    ``today`` picks the price row that is in force.
    """

    text = clean_query(query, limit=limit)
    day = today or (clock() if clock else datetime.now()).date()
    use_home = fetch is None and cache_dir is None
    path = _cache_path(cache_dir, "nhi-A21030000I-E41001-001.csv", use_home=use_home)
    getter = fetch or (lambda url, timeout_: default_get(url, timeout_, max_bytes=NHI_MAX_BYTES))
    body = load_cached(
        NHI_ENDPOINT,
        path,
        getter,
        max_age_s=CACHE_MAX_AGE_S,
        max_bytes=NHI_MAX_BYTES,
        timeout=timeout,
        note=note,
        label="Downloading the NHI drug-price file (data.gov.tw dataset 23715, about 96 MB). It is cached for 7 days.",
        what="NHI",
    )
    rows = _finish(path, body, open_nhi_csv)
    return search_nhi(rows, text, limit, day)
