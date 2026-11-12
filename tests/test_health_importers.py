"""Importers: dates (incl. ROC calendar), text, PDF (optional), FHIR R4, lab tables, limits."""

from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

from muse.health import crypto, importers
from muse.health.importers import (
    ImportedRecord,
    ImportFailed,
    find_single_date,
    import_fhir,
    import_file,
    import_lab_table,
    import_text,
    parse_date,
)
from muse.health.vault import LocalVault

# ---------------------------------------------------------------- dates


@pytest.mark.parametrize("value,expected", [
    ("2024-05-02", "2024-05-02"),
    ("2024/5/2", "2024-05-02"),
    ("2024.05.02", "2024-05-02"),
    ("2024-05-02T08:30:00+08:00", "2024-05-02"),
    ("2024-05-02T23:30:00Z", "2024-05-02"),
    ("2024年5月2日", "2024-05-02"),
    ("民國113年5月2日", "2024-05-02"),
    ("民國 113 年 05 月 02 日", "2024-05-02"),
    ("民國99年5月2日", "2010-05-02"),
    ("113年5月2日", "2024-05-02"),
    ("113/05/02", "2024-05-02"),
    ("113.5.2", "2024-05-02"),
    ("113-05-02", "2024-05-02"),
    ("１１３/０５/０２", "2024-05-02"),     # full-width digits
    # ambiguous or invalid -> None
    ("05/02/2024", None), ("02/05/24", None), ("99/05/02", None), ("2024-05", None), ("2024", None),
    ("113/02/30", None), ("2024-13-01", None), ("", None), ("next Tuesday", None), ("1130502", None),
])
def test_parse_date(value: str, expected: str | None) -> None:
    assert parse_date(value) == expected


def test_parse_date_compact_only_when_asked() -> None:
    assert parse_date("1130502", compact=True) == "2024-05-02"
    assert parse_date("20240502", compact=True) == "2024-05-02"
    assert parse_date("20240502") is None
    assert parse_date("12345678", compact=True) is None
    assert parse_date(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("text,expected", [
    ("檢驗日期：民國113年5月2日\n血糖 98", "2024-05-02"),
    ("採檢 113/05/02 空腹血糖 98", "2024-05-02"),
    ("Collected 2024-05-02, reported 2024-05-02", "2024-05-02"),
    ("Collected 2024-05-02, reported 2024-05-04", None),
    ("回診 113/05/02，上次 113/02/01", None),
    ("血壓 130/80 mmHg，心跳 72", None),
    ("05/02/2024", None),
    ("no dates here", None),
])
def test_find_single_date(text: str, expected: str | None) -> None:
    assert find_single_date(text) == expected


# ---------------------------------------------------------------- text


def test_markdown_title_kind_and_date() -> None:
    md = "# 血液檢查報告\n\n檢驗日期：民國113年5月2日\n\n| 項目 | 結果 |\n| 血糖 | 98 |\n"
    (rec,) = import_text(md.encode(), "report.md", source="台大醫院")
    assert rec == ImportedRecord("lab", "血液檢查報告", md, "2024-05-02", "台大醫院")


def test_text_defaults_conservative() -> None:
    (rec,) = import_text(b"Felt dizzy after lunch.\nAsk doctor about it.", "diary.txt")
    assert rec.kind == "note" and rec.title == "Felt dizzy after lunch." and rec.occurred_on is None


def test_text_ambiguous_kind_falls_back() -> None:
    (rec,) = import_text("# 檢驗 與 用藥 總整理\n內容".encode(), "x.md")
    assert rec.kind == "note"


def test_text_encodings_and_control_chars() -> None:
    big5 = "健康檢查結果：正常".encode("cp950")
    (rec,) = import_text(big5, "a.txt")
    assert rec.text == "健康檢查結果：正常"
    (rec,) = import_text("中文".encode("utf-16"), "b.txt")
    assert rec.text == "中文"
    (rec,) = import_text(b"\xef\xbb\xbfhello\r\nworld\x00\x07", "c.txt")
    assert rec.text == "hello\nworld"
    (rec,) = import_text(b"ok \xff\xfe\xfa broken", "d.txt")
    assert "ok" in rec.text


def test_text_title_sanitised_and_bounded() -> None:
    (rec,) = import_text(("\u202e" + "標" * 300 + "\n本文").encode(), "x.txt")
    assert "\u202e" not in rec.title and len(rec.title) <= 200
    (rec,) = import_text(b"   \n\n# \nbody", "2024-05-02 visit.txt")
    assert rec.title == "body" and rec.occurred_on == "2024-05-02"


def test_text_limits() -> None:
    with pytest.raises(ImportFailed):
        import_text(b"", "e.txt")
    with pytest.raises(ImportFailed):
        import_text(b"  \n \n", "e.txt")
    with pytest.raises(ImportFailed):
        import_text(b"x" * (importers.MAX_INPUT_BYTES + 1), "big.txt")
    with pytest.raises(ImportFailed) as exc:
        import_text(b"x" * (importers.MAX_TEXT_BYTES + 1), "big.txt")
    assert "2 MB" in str(exc.value)
    for magic in (b"PK\x03\x04rest", b"\x1f\x8b\x08rest"):
        with pytest.raises(ImportFailed):
            import_file(magic, "bomb.zip")


# ---------------------------------------------------------------- PDF


def test_pdf_without_pypdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(ImportFailed) as exc:
        import_file(b"%PDF-1.7 ...", "x.pdf")
    assert "pypdf" in str(exc.value)


def _fake_pypdf(pages: list[str], encrypted: bool = False) -> types.ModuleType:
    mod = types.ModuleType("pypdf")

    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class PdfReader:
        def __init__(self, stream: object) -> None:
            self.is_encrypted = encrypted
            self.pages = [Page(p) for p in pages]

    mod.PdfReader = PdfReader  # type: ignore[attr-defined]
    return mod


def test_pdf_with_text_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["病理報告\n檢體：乳房切片", "診斷：良性  民國113年5月2日"]))
    (rec,) = import_file(b"%PDF-1.7 fake", "scan.pdf", source="醫院")
    assert rec.kind == "pathology_report" and rec.title == "病理報告" and rec.occurred_on == "2024-05-02"


def test_pdf_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["", ""]))
    with pytest.raises(ImportFailed) as exc:
        import_file(b"%PDF-", "scan.pdf")
    assert "OCR" in str(exc.value)
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["x"] * (importers.MAX_PDF_PAGES + 1)))
    with pytest.raises(ImportFailed):
        import_file(b"%PDF-", "long.pdf")
    monkeypatch.setitem(sys.modules, "pypdf", _fake_pypdf(["x"], encrypted=True))
    with pytest.raises(ImportFailed):
        import_file(b"%PDF-", "locked.pdf")
    broken = types.ModuleType("pypdf")

    def boom(_: object) -> None:
        raise RuntimeError("malformed xref with bytes \x00\x01")

    broken.PdfReader = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", broken)
    with pytest.raises(ImportFailed) as exc:
        import_file(b"%PDF-", "bad.pdf")
    assert "\x00" not in str(exc.value)


# ---------------------------------------------------------------- FHIR


def _obs(oid: str, code: str, value: float, unit: str, when: str, low: float | None = None, high: float | None = None,
         category: str = "laboratory") -> dict:
    o = {
        "resourceType": "Observation", "id": oid, "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": category}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": "x", "display": code}]},
        "effectiveDateTime": when, "valueQuantity": {"value": value, "unit": unit},
    }
    if low is not None:
        o["referenceRange"] = [{"low": {"value": low, "unit": unit}, "high": {"value": high, "unit": unit}}]
    return o


def _bundle() -> dict:
    report_text = base64.b64encode("影像所見：肺部無明顯異常。".encode()).decode()
    note = base64.b64encode("出院摘要：病人狀況穩定。".encode()).decode()
    entries = [
        {"fullUrl": "urn:uuid:obs-1", "resource": _obs("o1", "Glucose", 98, "mg/dL", "2024-05-02T08:00:00+08:00", 70, 99)},
        {"resource": _obs("o2", "HbA1c", 5.6, "%", "2024-05-02T08:00:00+08:00")},
        {"resource": {"resourceType": "DiagnosticReport", "id": "dr1", "status": "final",
                      "category": [{"coding": [{"code": "LAB"}]}], "code": {"text": "血液生化檢查"},
                      "effectiveDateTime": "2024-05-02", "result": [{"reference": "urn:uuid:obs-1"}, {"reference": "Observation/o2"}],
                      "conclusion": "大致正常"}},
        {"resource": {"resourceType": "DiagnosticReport", "id": "dr2", "category": [{"coding": [{"code": "RAD"}]}],
                      "code": {"coding": [{"display": "Chest X-ray"}]}, "issued": "2024-03-01T10:00:00Z",
                      "presentedForm": [{"contentType": "text/plain", "data": report_text}]}},
        {"resource": _obs("o3", "Cholesterol", 180, "mg/dL", "2024-01-10")},
        {"resource": _obs("o4", "Cholesterol", 175, "mg/dL", "2024-02-10")},
        {"resource": {**_obs("o5", "Blood pressure", 0, "", "2024-02-10", category="vital-signs"), "valueQuantity": None,
                      "component": [{"code": {"text": "Systolic"}, "valueQuantity": {"value": 130, "unit": "mmHg"}},
                                    {"code": {"text": "Diastolic"}, "valueQuantity": {"value": 80, "unit": "mmHg"}}]}},
        {"resource": {"resourceType": "MedicationStatement", "status": "active",
                      "medicationCodeableConcept": {"text": "Metformin 500 mg"}, "dosage": [{"text": "早晚飯後各一顆"}],
                      "effectivePeriod": {"start": "2023-11-01"}}},
        {"resource": {"resourceType": "MedicationRequest", "status": "active", "authoredOn": "2024-04-01",
                      "medicationCodeableConcept": {"coding": [{"display": "Amlodipine 5 mg"}]}}},
        {"resource": {"resourceType": "Encounter", "status": "finished", "type": [{"text": "心臟內科門診"}],
                      "period": {"start": "2024-04-01T09:00:00+08:00"}, "serviceProvider": {"display": "示範醫院"},
                      "reasonCode": [{"text": "高血壓追蹤"}]}},
        {"resource": {"resourceType": "DocumentReference", "status": "current", "description": "出院摘要",
                      "date": "2024-02-20T12:00:00Z", "content": [{"attachment": {"contentType": "text/plain", "data": note}},
                                                                  {"attachment": {"contentType": "image/png", "data": "iVBORw0KGgo="}}]}},
        {"resource": {"resourceType": "Patient", "id": "p1", "name": [{"text": "王小明"}]}},
        {"resource": {"resourceType": "Observation"}},   # nearly empty
        "not a dict",
    ]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def test_fhir_bundle() -> None:
    recs = import_fhir(json.dumps(_bundle()).encode(), source="FHIR export", locale="zh-TW")
    by_kind: dict[str, list[ImportedRecord]] = {}
    for r in recs:
        by_kind.setdefault(r.kind, []).append(r)
    (lab_report,) = [r for r in by_kind["lab"] if r.title == "血液生化檢查"]
    assert lab_report.occurred_on == "2024-05-02"
    assert "Glucose: 98 mg/dL" in lab_report.text and "70 mg/dL–99 mg/dL" in lab_report.text and "HbA1c: 5.6 %" in lab_report.text
    assert "大致正常" in lab_report.text
    (img,) = by_kind["imaging_report"]
    assert img.title == "Chest X-ray" and img.occurred_on == "2024-03-01" and "肺部無明顯異常" in img.text
    grouped = sorted((r.occurred_on, r.title) for r in by_kind["lab"] if r is not lab_report)
    assert ("2024-01-10", "檢驗結果 2024-01-10") in grouped and ("2024-02-10", "檢驗結果 2024-02-10") in grouped
    assert all("Glucose" not in r.text for r in by_kind["lab"] if r is not lab_report)   # consumed by the report
    (vitals,) = by_kind["measurement"]
    assert "Systolic: 130 mmHg" in vitals.text and vitals.occurred_on == "2024-02-10"
    (meds,) = by_kind["medication_list"]
    assert "Metformin 500 mg: 早晚飯後各一顆 (active)" in meds.text and "Amlodipine 5 mg" in meds.text
    assert meds.occurred_on == "2024-04-01"
    (visit,) = by_kind["visit"]
    assert visit.occurred_on == "2024-04-01" and "高血壓追蹤" in visit.text and "示範醫院" in visit.text
    (doc,) = by_kind["document"]
    assert doc.title == "出院摘要" and doc.occurred_on == "2024-02-20" and "病人狀況穩定" in doc.text
    assert "王小明" not in "".join(r.text for r in recs)   # Patient resources are skipped
    assert all(r.source == "FHIR export" for r in recs)


def test_fhir_english_titles_and_single_resource() -> None:
    recs = import_fhir(_obs("o1", "Glucose", 98, "mg/dL", "2024-05-02"), locale="en")
    assert recs == [ImportedRecord("lab", "Lab results 2024-05-02", "Glucose: 98 mg/dL", "2024-05-02", "FHIR")]
    no_date = import_fhir({"resourceType": "Observation", "code": {"text": "Weight"}, "valueString": "70 kg"}, locale="en")
    assert no_date[0].occurred_on is None and no_date[0].title == "Lab results"


def test_fhir_dispatch_and_errors() -> None:
    recs = import_file(json.dumps(_bundle()).encode("utf-8"), "export.json")
    assert len(recs) >= 8
    with pytest.raises(ImportFailed):
        import_fhir({"no": "resourceType"})
    with pytest.raises(ImportFailed):
        import_fhir({"resourceType": "Bundle", "entry": "x"})
    with pytest.raises(ImportFailed):
        import_file(b"{\"resourceType\": ", "broken.json")


def test_fhir_too_many_records() -> None:
    entries = [{"resource": {"resourceType": "DiagnosticReport", "code": {"text": f"r{i}"}, "conclusion": "ok"}}
               for i in range(importers.MAX_RECORDS + 1)]
    with pytest.raises(ImportFailed) as exc:
        import_fhir({"resourceType": "Bundle", "entry": entries})
    assert "split" in str(exc.value)


def test_deeply_nested_json_rejected() -> None:
    deep = b"[" * 100000 + b"]" * 100000
    with pytest.raises(ImportFailed) as exc:
        import_file(deep, "deep.json")
    assert "nested" in str(exc.value)
    # Brackets inside strings do not count.
    ok = json.dumps([{"test": "x" + "[" * 1000, "value": "1"}]).encode()
    assert import_lab_table(ok)[0].text.startswith("x[[[")


def test_fhir_non_utf8_and_huge_attachment() -> None:
    bundle = {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "DiagnosticReport", "code": {"text": "報告"}, "conclusion": "正常"}}]}
    data = json.dumps(bundle, ensure_ascii=False).encode("cp950")
    (rec,) = import_file(data, "big5.json")
    assert rec.title == "報告" and "正常" in rec.text
    huge = base64.b64encode(b"x" * (importers.MAX_TEXT_BYTES + 10)).decode()
    doc = {"resourceType": "DocumentReference", "content": [{"attachment": {"contentType": "text/plain", "data": huge}}]}
    with pytest.raises(ImportFailed):
        import_fhir(doc)


# ---------------------------------------------------------------- lab tables


def test_lab_csv_english() -> None:
    csv_text = ("Date,Test Name,Value,Unit,Reference Range\n"
                "2024-05-02,Glucose,98,mg/dL,70-99\n"
                "2024-05-02,HbA1c,5.6,%,<5.7\n"
                "2024-01-10,Cholesterol,180,mg/dL,<200\n"
                ",Ferritin,50,ng/mL,\n"
                ",,,,\n")
    recs = import_lab_table(csv_text.encode(), "labs.csv", source="Clinic portal", locale="en")
    assert [r.occurred_on for r in recs] == [None, "2024-01-10", "2024-05-02"]
    may = recs[-1]
    assert may.kind == "lab" and may.title == "Lab results 2024-05-02" and may.source == "Clinic portal"
    assert may.text == "Glucose: 98 mg/dL (ref 70-99)\nHbA1c: 5.6 % (ref <5.7)"


def test_lab_csv_chinese_headers_roc_dates_tsv_big5() -> None:
    tsv = "檢驗日期\t檢驗項目\t檢驗結果\t單位\t參考值\n1130502\t血糖\t98\tmg/dL\t70-99\n113/05/02\t肌酸酐\t1.0\tmg/dL\t0.7-1.3\n"
    for data in (tsv.encode("utf-8"), tsv.encode("cp950")):
        (rec,) = import_lab_table(data, "健康存摺.tsv")
        assert rec.occurred_on == "2024-05-02" and rec.title == "檢驗結果 2024-05-02"
        assert "血糖: 98 mg/dL (參考值 70-99)" in rec.text and "肌酸酐: 1.0" in rec.text


def test_lab_json_shapes() -> None:
    rows = [{"date": "2024-05-02", "test": "Glucose", "value": 98, "unit": "mg/dL"}]
    assert import_lab_table(rows, locale="en")[0].text == "Glucose: 98 mg/dL"
    wrapped = json.dumps({"patient": "x", "results": rows}).encode()
    assert import_file(wrapped, "labs.json", locale="en")[0].occurred_on == "2024-05-02"
    with pytest.raises(ImportFailed):
        import_lab_table({"a": rows, "b": rows})
    with pytest.raises(ImportFailed):
        import_lab_table([1, 2, 3])  # type: ignore[list-item]


def test_lab_table_errors_and_limits() -> None:
    with pytest.raises(ImportFailed) as exc:
        import_lab_table(b"foo,bar\n1,2\n", "x.csv")
    assert "column" in str(exc.value)
    with pytest.raises(ImportFailed):
        import_lab_table(b"test,value\n", "x.csv")
    many_dates = "date,test,value\n" + "".join(f"{2000 + i // 365}-{(i // 28) % 12 + 1:02d}-{i % 28 + 1:02d},t,1\n" for i in range(importers.MAX_RECORDS + 50))
    with pytest.raises(ImportFailed):
        import_lab_table(many_dates.encode(), "x.csv")


def test_lab_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importers, "MAX_TABLE_ROWS", 10)
    with pytest.raises(ImportFailed):
        import_lab_table(("test,value\n" + "a,1\n" * 11).encode(), "x.csv")


# ---------------------------------------------------------------- end to end


def test_imported_records_fit_the_vault(tmp_path: Path) -> None:
    vault = LocalVault.create(tmp_path / "v.sqlite", "correct horse battery", kdf=crypto.INSECURE_TEST_KDF)
    recs = import_fhir(_bundle()) + import_text("# 門診紀錄\n民國113年5月2日 回診".encode(), "n.md")
    for r in recs:
        vault.add(*r)
    assert len(vault.list()) == len(recs)
    assert vault.search("血糖") or vault.search("Glucose")
