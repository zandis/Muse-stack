"""Turn files into records: (kind, title, text, occurred_on, source) tuples.

Pure functions over bytes/str. Nothing here touches the vault, the network or
the disk; the caller adds the results with vault.add(*record).

Supported: plain text / Markdown; PDF text (only if `pypdf` is installed, no
OCR); FHIR R4 JSON (Bundle or single resource); a generic lab table (CSV or
JSON: date, test name, value, unit, reference range).

Taiwan's My Health Bank (健康存摺): the public material we could find says the
NHIA offers the download as XML (and to SDK-integrated apps as JSON after
decryption), but we found no published field-level specification. Rather than
invent one, there is no dedicated importer; a CSV/JSON lab table exported or
copied from it goes through the generic table path, which recognises common
Chinese column names (檢驗日期, 檢驗項目, 檢驗結果, 單位, 參考值).

Guessing is conservative: the kind falls back to "note"/"document"/"lab"
by format, and a date is only taken from free text when exactly one
unambiguous date appears (ISO, 民國 years, or 3-digit ROC years like
113/05/02). D/M/Y vs M/D/Y orders and 2-digit years are never guessed.

Limits: input size, records per file, text per record, JSON nesting depth;
compressed archives (zip, gzip) are refused rather than unpacked.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import unicodedata
from datetime import date
from typing import Any, NamedTuple

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_RECORDS = 500
MAX_TEXT_BYTES = 2 * 1024 * 1024      # must not exceed vault.MAX_TEXT_BYTES
MAX_TITLE_CHARS = 200                 # vault.MAX_TITLE_CHARS
MAX_JSON_DEPTH = 64
MAX_PDF_PAGES = 500
MAX_TABLE_ROWS = 50000


class ImportFailed(ValueError):
    """The file cannot be imported. Messages never quote the file's content."""


class ImportedRecord(NamedTuple):
    kind: str
    title: str
    text: str
    occurred_on: str | None
    source: str


# ---------------------------------------------------------------- dates


def _mkdate(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


_ISO = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_ROC_FULL = re.compile(r"民國\s*(\d{1,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ROC_CJK = re.compile(r"(?<![\d民])(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ROC_NUM = re.compile(r"(?<![\d/.-])(\d{3})[/.-](\d{1,2})[/.-](\d{1,2})(?![\d/.-])")
_WEST_CJK = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def parse_date(value: str, *, compact: bool = False) -> str | None:
    """A single date value -> YYYY-MM-DD, or None if absent or ambiguous.

    Accepts 2024-05-02 (also / and .), ISO datetimes (the date as written),
    2024年5月2日, 民國113年5月2日, 113年5月2日, 113/05/02 (ROC, 3-digit year).
    With compact=True (structured columns only) also 20240502 and 1130502.
    """
    if not isinstance(value, str):
        return None
    s = unicodedata.normalize("NFKC", value).strip()
    if not s or len(s) > 40:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?", s)
    if m:
        return _mkdate(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})(?:\s+[0-9:]+)?", s)
    if m:
        return _mkdate(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(?:民國\s*)(\d{1,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if m:
        return _mkdate(int(m[1]) + 1911, int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if m:
        return _mkdate(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if m:
        return _mkdate(int(m[1]) + 1911, int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{3})[/.-](\d{1,2})[/.-](\d{1,2})(?:\s+[0-9:]+)?", s)
    if m and int(m[1]) >= 1:
        return _mkdate(int(m[1]) + 1911, int(m[2]), int(m[3]))
    if compact:
        if re.fullmatch(r"\d{8}", s) and s[:2] in ("19", "20"):
            return _mkdate(int(s[:4]), int(s[4:6]), int(s[6:]))
        if re.fullmatch(r"\d{7}", s):
            return _mkdate(int(s[:3]) + 1911, int(s[3:5]), int(s[5:]))
    return None


def find_single_date(text: str) -> str | None:
    """The one date in free text, or None when there is none or several."""
    s = unicodedata.normalize("NFKC", text)
    found: set[str] = set()
    for rx, offset in ((_ISO, 0), (_WEST_CJK, 0), (_ROC_FULL, 1911), (_ROC_CJK, 1911), (_ROC_NUM, 1911)):
        for m in rx.finditer(s):
            d = _mkdate(int(m[1]) + offset, int(m[2]), int(m[3]))
            if d:
                found.add(d)
            if len(found) > 1:
                return None
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------- shared helpers


def _decode(data: bytes) -> str:
    """UTF-8 (with or without BOM), UTF-16 with BOM, then Big5/CP950 (common
    in older Taiwanese systems), else UTF-8 with replacement characters."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp950")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _check_input(data: bytes | str) -> bytes:
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > MAX_INPUT_BYTES:
        raise ImportFailed(f"file is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB")
    if raw.startswith((b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!", b"BZh")):
        raise ImportFailed("compressed archives are not imported; extract the file first")
    return raw


def _clean_title(title: str, fallback: str) -> str:
    t = "".join(ch for ch in unicodedata.normalize("NFC", title or "") if unicodedata.category(ch)[0] != "C")
    t = " ".join(t.split()).strip(" #*-")
    if not t:
        t = fallback
    return t[:MAX_TITLE_CHARS]


def _clean_text(text: str) -> str:
    """Drop control characters except newline and tab; normalise line ends."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ImportFailed("a record would be larger than 2 MB; split the file")
    return text


def _clean_source(source: str) -> str:
    return _clean_title(source, "")[:300]


_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pathology_report", ("病理", "pathology", "biopsy", "切片")),
    ("imaging_report", ("影像", "放射", "x光", "x-ray", "radiology", "ct ", "mri", "超音波", "ultrasound", "電腦斷層", "核磁")),
    ("medication_list", ("用藥", "藥單", "藥物清單", "medication list", "medications", "處方")),
    ("lab", ("檢驗", "血液", "lab result", "laboratory", "生化", "hba1c", "血糖", "血脂")),
    ("visit", ("門診紀錄", "就診紀錄", "出院摘要", "visit note", "discharge summary", "progress note")),
)


def guess_kind(title: str, filename: str, default: str) -> str:
    """Only from the title and file name, and only if exactly one kind matches."""
    hay = f" {title} {filename} ".casefold()
    hits = {kind for kind, words in _KIND_WORDS if any(w in hay for w in words)}
    return hits.pop() if len(hits) == 1 else default


def _check_json_depth(text: str, limit: int = MAX_JSON_DEPTH) -> None:
    """Scan bracket nesting before json.loads, so deep nesting is a clean error
    instead of a RecursionError deep in the parser."""
    if text.count("[") + text.count("{") <= limit:
        return
    skeleton = _JSON_STRING.sub("", text)
    depth = 0
    for ch in _BRACKETS.findall(skeleton):
        if ch in "[{":
            depth += 1
            if depth > limit:
                raise ImportFailed("JSON is nested too deeply")
        else:
            depth -= 1


_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_BRACKETS = re.compile(r"[\[\]{}]")


def _load_json(raw: bytes) -> Any:
    text = _decode(raw)
    _check_json_depth(text)
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise ImportFailed("the file is not valid JSON") from None


def _limit(records: list[ImportedRecord]) -> list[ImportedRecord]:
    if len(records) > MAX_RECORDS:
        raise ImportFailed(f"the file would produce more than {MAX_RECORDS} records; split it")
    return records


# ---------------------------------------------------------------- dispatcher


def import_file(data: bytes, filename: str = "", *, source: str = "", locale: str = "zh-TW") -> list[ImportedRecord]:
    """Pick an importer by extension, then by content sniffing."""
    raw = _check_input(data)
    name = (filename or "").lower()
    if name.endswith(".pdf") or raw.startswith(b"%PDF-"):
        return import_pdf(raw, filename, source=source)
    if name.endswith((".csv", ".tsv")):
        return import_lab_table(raw, filename, source=source, locale=locale)
    if name.endswith(".json") or raw.lstrip()[:1] in (b"{", b"["):
        obj = _load_json(raw)
        if isinstance(obj, dict) and isinstance(obj.get("resourceType"), str):
            return import_fhir(obj, source=source, locale=locale)
        return import_lab_table(obj, filename, source=source, locale=locale)
    return import_text(raw, filename, source=source)


# ---------------------------------------------------------------- text / markdown


def import_text(data: bytes | str, filename: str = "", *, source: str = "") -> list[ImportedRecord]:
    raw = _check_input(data)
    text = _clean_text(_decode(raw))
    if not text.strip():
        raise ImportFailed("the file is empty")
    title = ""
    for line in text.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            title = candidate
            break
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename.rsplit("/", 1)[-1]) or "Note"
    title = _clean_title(title[:120], stem)
    kind = guess_kind(title, filename, "note")
    occurred = find_single_date(f"{filename}\n{text[:4000]}")
    return [ImportedRecord(kind, title, text, occurred, _clean_source(source))]


# ---------------------------------------------------------------- PDF


def import_pdf(data: bytes, filename: str = "", *, source: str = "") -> list[ImportedRecord]:
    """Text layer only. Scanned PDFs without text produce an error, not OCR."""
    raw = _check_input(data)
    try:
        import pypdf  # type: ignore[import-not-found]
    except ImportError:
        raise ImportFailed("PDF import needs the optional 'pypdf' package (pip install 'muse-stack[pdf]')") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ImportFailed("the PDF is password-protected")
        pages = reader.pages
        if len(pages) > MAX_PDF_PAGES:
            raise ImportFailed(f"the PDF has more than {MAX_PDF_PAGES} pages")
        parts: list[str] = []
        total = 0
        for page in pages:
            chunk = page.extract_text() or ""
            total += len(chunk.encode("utf-8"))
            if total > MAX_TEXT_BYTES:
                raise ImportFailed("the PDF text is larger than 2 MB; split it")
            parts.append(chunk)
    except ImportFailed:
        raise
    except Exception:  # pypdf raises many types on malformed input
        raise ImportFailed("the PDF could not be read") from None
    text = _clean_text("\n\n".join(parts))
    if not text.strip():
        raise ImportFailed("the PDF has no text layer (scanned image); OCR is not supported")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    stem = re.sub(r"\.pdf$", "", filename.rsplit("/", 1)[-1], flags=re.IGNORECASE) or "PDF document"
    title = _clean_title(first[:120], stem)
    kind = guess_kind(title, filename, "document")
    return [ImportedRecord(kind, title, text, find_single_date(f"{filename}\n{text[:4000]}"), _clean_source(source))]


# ---------------------------------------------------------------- FHIR R4

_T = {
    "zh": {"lab": "檢驗結果", "meds": "用藥清單", "visit": "就診", "report": "報告", "doc": "文件", "ref": "參考值",
           "conclusion": "結論", "meas": "量測結果"},
    "en": {"lab": "Lab results", "meds": "Medication list", "visit": "Visit", "report": "Report", "doc": "Document",
           "ref": "ref", "conclusion": "Conclusion", "meas": "Measurements"},
}


def _lang(locale: str) -> dict[str, str]:
    return _T["zh" if (locale or "").lower().startswith("zh") else "en"]


def _cc_text(cc: Any) -> str:
    """CodeableConcept -> display text."""
    if isinstance(cc, list):
        return ", ".join(t for t in (_cc_text(c) for c in cc[:5]) if t)
    if not isinstance(cc, dict):
        return ""
    if isinstance(cc.get("text"), str) and cc["text"].strip():
        return cc["text"].strip()
    for coding in cc.get("coding") or []:
        if isinstance(coding, dict):
            for key in ("display", "code"):
                if isinstance(coding.get(key), str) and coding[key].strip():
                    return coding[key].strip()
    return ""


def _codes(cc: Any) -> set[str]:
    out: set[str] = set()
    for c in cc if isinstance(cc, list) else [cc]:
        if isinstance(c, dict):
            for coding in c.get("coding") or []:
                if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                    out.add(coding["code"].lower())
            if isinstance(c.get("text"), str):
                out.add(c["text"].lower())
    return out


def _fhir_date(res: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = res.get(key)
        if isinstance(value, dict):   # Period
            value = value.get("start")
        if isinstance(value, str):
            d = parse_date(value)
            if d:
                return d
    return None


def _quantity(q: Any) -> str:
    if not isinstance(q, dict):
        return ""
    value = q.get("value")
    unit = q.get("unit") or q.get("code") or ""
    comparator = q.get("comparator") or ""
    if value is None:
        return ""
    return f"{comparator}{value} {unit}".strip()


def _obs_value(obs: dict[str, Any]) -> str:
    if "valueQuantity" in obs:
        return _quantity(obs["valueQuantity"])
    for key in ("valueString", "valueInteger", "valueBoolean", "valueDateTime"):
        if key in obs:
            return str(obs[key])
    if "valueCodeableConcept" in obs:
        return _cc_text(obs["valueCodeableConcept"])
    if isinstance(obs.get("dataAbsentReason"), dict):
        return f"({_cc_text(obs['dataAbsentReason'])})"
    return ""


def _obs_line(obs: dict[str, Any], t: dict[str, str]) -> str:
    name = _cc_text(obs.get("code")) or "?"
    value = _obs_value(obs)
    parts = [f"{name}: {value}" if value else name]
    ranges = []
    for rr in (obs.get("referenceRange") or [])[:3]:
        if not isinstance(rr, dict):
            continue
        if isinstance(rr.get("text"), str):
            ranges.append(rr["text"])
        else:
            low, high = _quantity(rr.get("low")), _quantity(rr.get("high"))
            if low or high:
                ranges.append(f"{low or ''}–{high or ''}")
    if ranges:
        parts.append(f"({t['ref']} {'; '.join(ranges)})")
    interp = _cc_text(obs.get("interpretation"))
    if interp:
        parts.append(f"[{interp}]")
    line = " ".join(parts)
    comps = [c for c in (obs.get("component") or []) if isinstance(c, dict)][:20]
    for comp in comps:
        line += f"\n  - {_cc_text(comp.get('code')) or '?'}: {_obs_value(comp)}"
    if isinstance(obs.get("note"), list):
        for note in obs["note"][:3]:
            if isinstance(note, dict) and isinstance(note.get("text"), str):
                line += f"\n  {note['text']}"
    return line


def _attachment_text(att: Any) -> str:
    """Decode a text/* attachment's inline data; ignore binaries and URLs."""
    if not isinstance(att, dict):
        return ""
    ctype = str(att.get("contentType") or "")
    data = att.get("data")
    if not isinstance(data, str) or not (ctype.startswith("text/") or ctype in ("", "application/json")):
        return ""
    if len(data) > MAX_TEXT_BYTES * 4 // 3 + 8:
        raise ImportFailed("an attachment is larger than 2 MB")
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return ""
    return _decode(raw)


def import_fhir(data: bytes | str | dict[str, Any], *, source: str = "", locale: str = "zh-TW") -> list[ImportedRecord]:
    """FHIR R4 Bundle or single resource -> records.

    One record per DiagnosticReport (with its referenced Observations), per
    Encounter and per DocumentReference; remaining Observations grouped by date
    (one "lab" or "measurement" record per day); all MedicationStatement /
    MedicationRequest resources as one medication_list. Other resources are
    skipped.
    """
    obj = data if isinstance(data, dict) else _load_json(_check_input(data))
    if not isinstance(obj, dict) or not isinstance(obj.get("resourceType"), str):
        raise ImportFailed("not a FHIR resource")
    t = _lang(locale)
    src = _clean_source(source or "FHIR")
    resources: list[dict[str, Any]] = []
    if obj["resourceType"] == "Bundle":
        entries = obj.get("entry") or []
        if not isinstance(entries, list):
            raise ImportFailed("malformed FHIR bundle")
        if len(entries) > MAX_TABLE_ROWS:
            raise ImportFailed("the FHIR bundle is too large; split it")
        by_ref: dict[str, dict[str, Any]] = {}
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("resource"), dict):
                r = e["resource"]
                resources.append(r)
                if isinstance(e.get("fullUrl"), str):
                    by_ref[e["fullUrl"]] = r
                if isinstance(r.get("id"), str) and isinstance(r.get("resourceType"), str):
                    by_ref[f"{r['resourceType']}/{r['id']}"] = r
    else:
        resources = [obj]
        by_ref = {}
    records: list[ImportedRecord] = []
    used: set[int] = set()
    meds: list[dict[str, Any]] = []
    obs_by_day: dict[tuple[str | None, str], list[dict[str, Any]]] = {}

    for r in resources:
        rtype = r.get("resourceType")
        if rtype == "DiagnosticReport":
            codes = _codes(r.get("category"))
            if codes & {"lab", "laboratory", "ch", "hm", "mb"}:
                kind = "lab"
            elif codes & {"rad", "imaging", "radiology", "ct", "mr", "us", "nmr"}:
                kind = "imaging_report"
            elif codes & {"path", "pat", "sp", "pathology", "cp"}:
                kind = "pathology_report"
            else:
                kind = "report"
            title = _clean_title(_cc_text(r.get("code")), t["report"])
            lines = []
            for ref in (r.get("result") or [])[:500]:
                target = by_ref.get(ref.get("reference", "")) if isinstance(ref, dict) else None
                if isinstance(target, dict) and target.get("resourceType") == "Observation":
                    used.add(id(target))
                    lines.append(_obs_line(target, t))
            if isinstance(r.get("conclusion"), str) and r["conclusion"].strip():
                lines.append(f"\n{t['conclusion']}: {r['conclusion'].strip()}")
            for form in (r.get("presentedForm") or [])[:5]:
                txt = _attachment_text(form)
                if txt.strip():
                    lines.append("\n" + txt)
            text = _clean_text("\n".join(lines))
            if text.strip():
                records.append(ImportedRecord(kind, title, text, _fhir_date(r, "effectiveDateTime", "effectivePeriod", "issued"), src))
        elif rtype in ("MedicationStatement", "MedicationRequest"):
            meds.append(r)
        elif rtype == "Encounter":
            kind_text = _cc_text(r.get("type")) or _cc_text(r.get("class")) or ""
            lines = [x for x in (
                kind_text,
                _cc_text(r.get("reasonCode")),
                (r.get("serviceProvider") or {}).get("display", "") if isinstance(r.get("serviceProvider"), dict) else "",
            ) if x]
            for p in (r.get("participant") or [])[:5]:
                if isinstance(p, dict) and isinstance(p.get("individual"), dict) and isinstance(p["individual"].get("display"), str):
                    lines.append(p["individual"]["display"])
            text = _clean_text("\n".join(lines))
            if text.strip():
                title = _clean_title(f"{t['visit']} {kind_text}".strip(), t["visit"])
                records.append(ImportedRecord("visit", title, text, _fhir_date(r, "period"), src))
        elif rtype == "DocumentReference":
            texts = [_attachment_text((c or {}).get("attachment")) for c in (r.get("content") or [])[:5] if isinstance(c, dict)]
            body = "\n\n".join(x for x in texts if x.strip())
            if isinstance(r.get("description"), str) and r["description"].strip():
                body = (r["description"].strip() + "\n\n" + body).strip()
            text = _clean_text(body)
            if text.strip():
                title = _clean_title(r.get("description") if isinstance(r.get("description"), str) else _cc_text(r.get("type")), t["doc"])
                records.append(ImportedRecord("document", title, text, _fhir_date(r, "date"), src))
        if len(records) > MAX_RECORDS:
            raise ImportFailed(f"the file would produce more than {MAX_RECORDS} records; split it")

    for r in resources:
        if r.get("resourceType") == "Observation" and id(r) not in used:
            if not _cc_text(r.get("code")) and not _obs_value(r):
                continue   # nothing readable to import
            day = _fhir_date(r, "effectiveDateTime", "effectivePeriod", "issued")
            kind = "measurement" if _codes(r.get("category")) & {"vital-signs", "vital signs"} else "lab"
            obs_by_day.setdefault((day, kind), []).append(r)
    for (day, kind), group in sorted(obs_by_day.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        label = t["lab"] if kind == "lab" else t["meas"]
        text = _clean_text("\n".join(_obs_line(o, t) for o in group))
        records.append(ImportedRecord(kind, _clean_title(f"{label} {day or ''}", label), text, day, src))

    if meds:
        lines = []
        dates = []
        for m in meds[:1000]:
            name = _cc_text(m.get("medicationCodeableConcept")) or (
                (m.get("medicationReference") or {}).get("display", "") if isinstance(m.get("medicationReference"), dict) else "")
            dose = "; ".join(d["text"] for d in (m.get("dosage") or m.get("dosageInstruction") or [])[:3]
                             if isinstance(d, dict) and isinstance(d.get("text"), str))
            status = m.get("status") if isinstance(m.get("status"), str) else ""
            line = f"- {name or '?'}"
            if dose:
                line += f": {dose}"
            if status:
                line += f" ({status})"
            lines.append(line)
            d = _fhir_date(m, "effectiveDateTime", "effectivePeriod", "authoredOn", "dateAsserted")
            if d:
                dates.append(d)
        records.append(ImportedRecord("medication_list", t["meds"], _clean_text("\n".join(lines)), max(dates) if dates else None, src))
    return _limit(records)


# ---------------------------------------------------------------- generic lab table

_COLS = {
    "date": ("date", "日期", "檢驗日期", "採檢日期", "報告日期", "collected", "collection date", "result date", "就醫日期"),
    "test": ("test", "test name", "name", "item", "檢驗項目", "項目", "項目名稱", "檢驗名稱", "analyte"),
    "value": ("value", "result", "結果", "檢驗結果", "數值", "檢驗值"),
    "unit": ("unit", "units", "單位"),
    "range": ("reference range", "ref range", "range", "reference", "參考值", "參考範圍", "參考區間"),
}


def _map_columns(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for h in headers:
        key = unicodedata.normalize("NFKC", str(h)).strip().casefold()
        for field, names in _COLS.items():
            if field not in mapping and key in names:
                mapping[field] = h
    if "test" not in mapping or "value" not in mapping:
        raise ImportFailed("no test-name and value columns found (expected e.g. 'test', 'value' or '檢驗項目', '檢驗結果')")
    return mapping


def _rows_from_json(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict):
        lists = [v for v in obj.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
        if len(lists) != 1:
            raise ImportFailed("expected a JSON list of rows")
        obj = lists[0]
    if not isinstance(obj, list) or not all(isinstance(x, dict) for x in obj):
        raise ImportFailed("expected a JSON list of rows")
    return obj


def import_lab_table(data: bytes | str | list[Any] | dict[str, Any], filename: str = "", *, source: str = "",
                     locale: str = "zh-TW") -> list[ImportedRecord]:
    """CSV/TSV or JSON rows -> one readable "lab" record per date."""
    if isinstance(data, (bytes, str)):
        raw = _check_input(data)
        stripped = raw.lstrip()
        if stripped[:1] in (b"{", b"["):
            rows = _rows_from_json(_load_json(raw))
        else:
            text = _decode(raw)
            sample = text[:4096]
            delim = max((",", "\t", ";"), key=sample.count)
            csv.field_size_limit(MAX_TEXT_BYTES)
            try:
                reader = csv.DictReader(io.StringIO(text), delimiter=delim)
                rows = []
                for i, row in enumerate(reader):
                    if i >= MAX_TABLE_ROWS:
                        raise ImportFailed("the table has too many rows; split it")
                    rows.append(row)
            except csv.Error:
                raise ImportFailed("the CSV could not be parsed") from None
    else:
        rows = _rows_from_json(data)
    if not rows:
        raise ImportFailed("the table is empty")
    if len(rows) > MAX_TABLE_ROWS:
        raise ImportFailed("the table has too many rows; split it")
    headers = [h for h in rows[0] if h is not None]
    cols = _map_columns(headers)
    t = _lang(locale)
    groups: dict[str | None, list[str]] = {}
    for row in rows:
        def cell(field: str, row: dict[str, Any] = row) -> str:
            v = row.get(cols[field]) if field in cols else None
            return "" if v is None else " ".join(str(v).split())
        name, value = cell("test"), cell("value")
        if not name and not value:
            continue
        day = parse_date(cell("date"), compact=True) if "date" in cols else None
        line = f"{name or '?'}: {value}"
        if cell("unit"):
            line += f" {cell('unit')}"
        if cell("range"):
            line += f" ({t['ref']} {cell('range')})"
        groups.setdefault(day, []).append(line)
    if len(groups) > MAX_RECORDS:
        raise ImportFailed(f"the file would produce more than {MAX_RECORDS} records; split it")
    src = _clean_source(source)
    records = []
    for day in sorted(groups, key=lambda d: d or ""):
        title = _clean_title(f"{t['lab']} {day or ''}", t["lab"])
        records.append(ImportedRecord("lab", title, _clean_text("\n".join(groups[day])), day, src))
    return _limit(records)
