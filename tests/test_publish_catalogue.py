"""scripts/publish_catalogue.py: the catalogue as dated public JSON (A15).

The committed files under catalogue/ must match what config/ gives, the same way
the conformance vectors are held to the Python functions. No network.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from muse import catalog, cli

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "catalogue"


def _load():
    spec = importlib.util.spec_from_file_location("publish_catalogue_under_test", ROOT / "scripts" / "publish_catalogue.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pub = _load()
CAT = catalog.load_catalog()
DOC = json.loads((OUT / pub.PRETTY).read_text(encoding="utf-8"))


def quiet(*_a, **_k) -> None:
    return None


def with_provider(cat: catalog.Catalog, provider_id: str, **raw_changes) -> catalog.Catalog:
    """The catalogue with one provider's YAML entry changed."""
    out = []
    for p in cat.providers:
        if p.id == provider_id:
            raw = copy.deepcopy(p.raw)
            raw.update(raw_changes)
            p = catalog.provider_from_dict(raw)
        out.append(p)
    return replace(cat, providers=tuple(out))


# ------------------------------------------------------------ cannot drift


def test_committed_files_match_config():
    lines: list[str] = []
    code = pub.run(check=True, say=lines.append)
    assert code == 0, (
        "catalogue/ is out of date with config/: run `python scripts/publish_catalogue.py` (or `muse catalog export`) "
        "and commit the result. " + " ".join(lines)
    )
    assert json.loads((OUT / pub.MINIFIED).read_text(encoding="utf-8")) == DOC


def test_check_notices_a_change_in_config_and_writes_nothing():
    before = {p.name: p.read_bytes() for p in OUT.iterdir() if p.is_file()}
    lines: list[str] = []
    changed = with_provider(CAT, "openrouter", checked_on="2026-11-13", allowance=[{"window": "day", "limit": 25, "unit": "requests"}])
    assert pub.run(check=True, cat=changed, say=lines.append) == 1
    assert "out of date" in lines[0] and "python scripts/publish_catalogue.py" in lines[0]
    assert {p.name: p.read_bytes() for p in OUT.iterdir() if p.is_file()} == before


def test_two_runs_give_the_same_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert pub.run(out_dir=a, say=quiet) == 0 and pub.run(out_dir=b, say=quiet) == 0
    assert pub.run(out_dir=a, say=quiet) == 0  # again over its own output: nothing moves
    for name in (pub.PRETTY, pub.MINIFIED, pub.CHANGELOG):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    assert pub.run(out_dir=a, check=True, say=quiet) == 0
    source = (ROOT / "scripts" / "publish_catalogue.py").read_text(encoding="utf-8")
    for clock in ("datetime.now", "date.today", "time.time", "utcnow", "import random", "uuid"):
        assert clock not in source, clock


def test_dates_come_from_the_catalogue_or_the_flag(tmp_path):
    doc, _ = pub.build(CAT)
    newest = max(d for d in [*(p.checked_on for p in CAT.providers), *(r.checked_on for r in CAT.resources), *(s.checked_on for s in CAT.subscriptions)] if d)
    assert doc["catalogue_date"] == doc["generated_at"] == newest == DOC["catalogue_date"]
    doc, _ = pub.build(CAT, date="2026-12-01")
    assert doc["generated_at"] == "2026-12-01" and doc["catalogue_date"] == newest
    assert pub.run(out_dir=tmp_path, date="tomorrow", say=quiet) == 2 and not list(tmp_path.iterdir())


# ------------------------------------------------------------------ schema


def test_schema_file_is_a_json_schema_and_the_document_matches_it():
    schema = pub.load_schema()
    assert schema["$schema"].startswith("https://json-schema.org/") and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(DOC) == {
        "schema_version", "schema", "generated_at", "catalogue_date", "release", "licence", "source", "counts",
        "ai_services", "other_allowances", "subscriptions", "changes",
    }
    assert pub.schema_problems(DOC, schema) == []
    assert DOC["schema_version"] == catalog.PUBLIC_SCHEMA_VERSION == "1.1" and (ROOT / DOC["schema"]).is_file()
    # every object in the schema is closed, so a new field cannot slip out unnoticed
    def closed(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, path
            for key, value in node.items():
                closed(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                closed(value, f"{path}[{i}]")
    closed(schema)


@pytest.mark.parametrize("mutate, word", [
    (lambda d: d.update(extra=1), "unexpected field extra"),
    (lambda d: d.pop("licence"), "licence is missing"),
    (lambda d: d["ai_services"][0].update(tier="gratis"), "is not one of"),
    (lambda d: d["ai_services"][0]["allowance"]["limits"].append({"window": "day"}), "limit is missing"),
    (lambda d: d["ai_services"][0]["connection"].update(test={"url": "https://example.com"}), "unexpected field test"),
    (lambda d: d["ai_services"][0]["connection"]["env_names"].append("not a name"), "does not match"),
    (lambda d: d["other_allowances"][0].update(checked_on="last week"), "does not match"),
    (lambda d: d["other_allowances"][0]["allowance"]["limits"][0].update(limit=0), "is below 1"),
    (lambda d: d["subscriptions"][0].update(handoff_only=False), "must be True"),
    (lambda d: d["ai_services"][0]["connection"]["steps"][0]["title"].pop("zh-TW"), "zh-TW is missing"),
    (lambda d: d.update(generated_at=20261113), "must be string"),
])
def test_schema_checker_catches(mutate, word):
    doc = copy.deepcopy(DOC)
    mutate(doc)
    problems = pub.schema_problems(doc, pub.load_schema())
    assert any(word in p for p in problems), problems


def test_schema_checker_refuses_a_keyword_it_does_not_know():
    assert "does not know" in pub.schema_problems(1, {"type": "integer", "multipleOf": 2})[0]
    assert "cannot resolve" in pub.schema_problems(1, {"$ref": "#/$defs/missing"})[0]


# ------------------------------------------------------- only public facts


def test_every_entry_is_there_with_its_date_and_sources():
    assert [e["id"] for e in DOC["ai_services"]] == sorted(p.id for p in CAT.providers)
    assert {e["id"] for e in DOC["other_allowances"]} == {r.id for r in CAT.resources}
    assert {e["kind"] for e in DOC["other_allowances"]} == {"storage", "compute", "chain", "messaging"}
    assert [e["id"] for e in DOC["subscriptions"]] == sorted(s.id for s in CAT.subscriptions) and all(e["handoff_only"] is True for e in DOC["subscriptions"])
    assert DOC["counts"] == {k: len(DOC[k]) for k in ("ai_services", "other_allowances", "subscriptions")}
    for entry in [*DOC["ai_services"], *DOC["other_allowances"], *DOC["subscriptions"]]:
        if entry.get("kind") == "local":
            continue
        assert re.fullmatch(r"\d{4}-\d\d-\d\d", entry["checked_on"]) and entry["confidence"] in ("H", "M", "L"), entry["id"]
        assert all(u.startswith("https://") for u in entry["source_urls"])
        if entry["confidence"] in ("H", "M"):
            assert entry["source_urls"], entry["id"]


def test_limits_are_marked_and_unknown_is_said_plainly():
    by_id = {e["id"]: e for e in DOC["ai_services"]}
    openrouter = by_id["openrouter"]["allowance"]
    assert openrouter["status"] == "verified" and all(item["status"] == "verified" for item in openrouter["limits"])
    assert {(i["window"], i["limit"], i["unit"]) for i in openrouter["limits"]} == {(a.window, a.limit, a.unit) for a in CAT.provider("openrouter").allowance}
    assert openrouter["reset"]["rule"] == "utc_day"
    empty = [e for e in DOC["ai_services"] if not e["allowance"]["limits"]]
    assert empty and all(e["allowance"]["status"] == "unknown" for e in empty)  # no number is invented
    # A quota the rationing code uses but no page confirmed is published as an estimate, never as verified.
    doc, _ = pub.build(with_provider(CAT, "gemini", quota={"requests_per_day": 123}))
    limits = {e["id"]: e for e in doc["ai_services"]}["gemini"]["allowance"]
    assert limits["status"] == "estimated" and limits["limits"] == [
        {"window": "day", "limit": 123, "unit": "requests", "scope": None, "condition": None, "headline": False, "status": "estimated"}
    ]
    assert pub.problems_of(doc) == []


def test_connection_steps_are_in_both_languages_and_minutes_are_labelled_estimates():
    for entry in [*DOC["ai_services"], *DOC["other_allowances"]]:
        conn = entry["connection"]
        assert conn["card_required"] in ("yes", "no", "unknown") and conn["phone_required"] in ("yes", "no", "unknown")
        for step in conn["steps"]:
            assert step["title"]["en"].strip() and step["title"]["zh-TW"].strip() and step["detail"]["en"].strip() and step["detail"]["zh-TW"].strip()
        if conn["minutes_estimate"] is not None:
            assert conn["minutes_estimate"]["basis"] == "estimate" and conn["minutes_estimate"]["minutes"] >= 1
    assert any(e["connection"]["minutes_estimate"] for e in DOC["ai_services"])
    assert any(e["connection"]["method"] == "oauth_pkce" for e in DOC["ai_services"])


def test_env_var_names_are_published_and_nothing_internal_is():
    raw = yaml.safe_load((ROOT / "config" / "providers.yaml").read_text(encoding="utf-8"))["providers"]
    text = (OUT / pub.PRETTY).read_text(encoding="utf-8")
    by_id = {e["id"]: e for e in DOC["ai_services"]}
    tests = 0
    for item in raw:
        entry = by_id[item["id"]]
        names = [n for n in (item.get("env"), *(item.get("also_env") or []), item.get("base_env")) if n]
        assert entry["connection"]["env_names"] == names, item["id"]
        test = (item.get("connection") or {}).get("test")
        if test:  # the key-check request is not published; a documented "what is left" address may be
            tests += 1
            documented = (item.get("read_remaining") or {}).get("endpoint")
            assert text.count(f'"{test["url"]}"') == (1 if documented == test["url"] else 0), item["id"]
            assert "test" not in entry["connection"]
        if item.get("api_base"):
            assert f'"{item["api_base"]}"' not in text
        assert "policy" in entry and entry["policy"]["trains_on_inputs"] in ("yes", "no", "unknown")
    assert tests >= 3
    keys = set()

    def walk(node):
        if isinstance(node, dict):
            keys.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(DOC)
    assert not keys & pub.INTERNAL_KEYS and "rpm" not in keys and "aliases" not in keys
    assert pub.guard(DOC) == [] and catalog.find_secretlike(DOC) == []
    for private in (str(ROOT), str(Path.home()), "MUSE_HOME", "ledger.db"):
        assert private not in text, private
    assert DOC["source"]["generator"] == "scripts/publish_catalogue.py" and all(not f.startswith("/") for f in DOC["source"]["files"])


@pytest.mark.parametrize("field, value, word", [
    ("notes", "the key is sk-" + "abcDEF0123456789abcDEF0123", "looks like a key or token"),
    ("notes", "Authorization: Bearer " + "abcdefghijklmnop0123456789", "looks like a key or token"),
    ("notes", "kept in /home/someone/.muse/ledger.db", "contains a local path"),
    ("notes", "see C:\\Users\\someone\\muse", "contains a local path"),
])
def test_a_secret_or_a_local_path_stops_the_build(tmp_path, field, value, word):
    lines: list[str] = []
    code = pub.run(out_dir=tmp_path, cat=with_provider(CAT, "groq", **{field: value}), say=lines.append)
    assert code == 2 and word in lines[0] and "ai_services" in lines[0]
    assert not list(tmp_path.iterdir())  # nothing is written
    assert value not in lines[0]  # and the value is not echoed


def test_guard_refuses_internal_field_names():
    doc = copy.deepcopy(DOC)
    doc["ai_services"][0]["api_base"] = "https://api.example.com"
    assert any("api_base is an internal field" in p for p in pub.guard(doc))


# ------------------------------------------------------------------ licence


def test_the_chosen_licence_is_stated_and_its_texts_are_in_the_repository():
    """The owner chose on 2026-11-15: the published catalogue is CC BY 4.0, the code Apache-2.0."""
    assert pub.LICENCE_SPDX == "CC-BY-4.0"
    assert "Apache License" in (ROOT / "LICENSE").read_text(encoding="utf-8")[:200]
    assert (OUT / "LICENSE").read_text(encoding="utf-8").startswith("Attribution 4.0 International")
    licence = DOC["licence"]
    assert licence["status"] == "chosen" and licence["spdx"] == "CC-BY-4.0" and licence["statement"] == "Licensed under CC-BY-4.0."
    assert [o["spdx"] for o in licence["options"]] == ["CC0-1.0", "CC-BY-4.0"] and all(o["summary"] for o in licence["options"])
    doc = (ROOT / "docs" / "catalogue.md").read_text(encoding="utf-8")
    assert "CC BY 4.0" in doc and "catalogue/LICENSE" in doc and "Apache-2.0" in doc
    assert "## Licence" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert DOC["release"] == {"status": "draft", "released_on": None}  # choosing a licence is not releasing
    assert not (OUT / pub.RELEASES).exists()


def test_an_undecided_licence_is_said_plainly_and_release_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pub, "LICENCE_SPDX", None)
    monkeypatch.setattr(cli, "_publisher", lambda: pub)  # the command line loads its own copy of the script
    licence = pub.licence_block()
    assert licence["status"] == "undecided" and licence["spdx"] is None
    assert licence["statement"] == "licence to be chosen by the owner before publication"
    lines: list[str] = []
    assert pub.run(out_dir=tmp_path, release=True, say=lines.append) == 2
    assert "refusing to release" in lines[0] and "licence to be chosen by the owner" in lines[0] and "LICENCE_SPDX" in lines[0]
    assert not list(tmp_path.iterdir())
    assert pub.main(["--out", str(tmp_path), "--release"]) == 2 and not list(tmp_path.iterdir())
    assert cli.main(["catalog", "export", "--out", str(tmp_path), "--release"]) == 2 and not list(tmp_path.iterdir())
    assert pub.run(out_dir=tmp_path, release=True, check=True, say=quiet) == 2


def test_release_works_once_a_licence_is_chosen_and_changes_start_again(tmp_path, monkeypatch):
    assert pub.run(out_dir=tmp_path, say=quiet) == 0
    monkeypatch.setattr(pub, "LICENCE_SPDX", "CC0-1.0")
    assert pub.run(out_dir=tmp_path, release=True, date="2026-12-01", say=quiet) == 0
    doc = json.loads((tmp_path / pub.PRETTY).read_text(encoding="utf-8"))
    assert doc["licence"] == {"status": "chosen", "spdx": "CC0-1.0", "statement": "Licensed under CC0-1.0.", "options": doc["licence"]["options"]}
    assert doc["release"] == {"status": "released", "released_on": "2026-12-01"} and pub.problems_of(doc) == []
    dated = tmp_path / pub.RELEASES / "catalogue.v1.2026-12-01.json"
    assert dated.read_bytes() == (tmp_path / pub.PRETTY).read_bytes()
    # regenerating a released document with nothing changed keeps it released
    assert pub.run(out_dir=tmp_path, date="2026-12-01", say=quiet) == 0
    assert json.loads((tmp_path / pub.PRETTY).read_text(encoding="utf-8"))["release"]["status"] == "released"
    # the first change after a release is a draft again, and changes[] holds only what is new since the release
    changed = with_provider(CAT, "groq", checked_on="2026-12-02")
    assert pub.run(out_dir=tmp_path, cat=changed, say=quiet) == 0
    doc = json.loads((tmp_path / pub.PRETTY).read_text(encoding="utf-8"))
    assert doc["release"]["status"] == "draft" and [c["change"] for c in doc["changes"]] == ["date_changed"]
    monkeypatch.setattr(pub, "LICENCE_SPDX", "MIT")
    assert pub.run(out_dir=tmp_path, say=quiet) == 2  # only the listed options


# ------------------------------------------------------------------ changes


def test_changes_since_the_previous_document():
    base, _ = pub.build(CAT)
    new = with_provider(CAT, "openrouter", checked_on="2026-11-14", confidence="M",
                        allowance=[{"window": "day", "limit": 25, "unit": "requests", "scope": "free models"}],
                        policy={**CAT.provider("openrouter").policy, "trains_on_inputs": "no"})
    new = replace(new, resources=tuple(r for r in new.resources if r.id != "google-drive"),
                  subscriptions=(*new.subscriptions, replace(new.subscriptions[0], id="another")))
    doc, fresh = pub.build(new, base, date="2026-11-14")
    got = {(c["change"], c["section"], c["id"], c["field"]) for c in fresh}
    assert got == {
        ("limits_changed", "ai_services", "openrouter", "allowance"),
        ("policy_changed", "ai_services", "openrouter", "policy.trains_on_inputs"),
        ("date_changed", "ai_services", "openrouter", "checked_on"),
        ("confidence_changed", "ai_services", "openrouter", "confidence"),
        ("removed", "other_allowances", "google-drive", None),
        ("added", "subscriptions", "another", None),
    }
    assert all(c["date"] == "2026-11-14" for c in fresh) and doc["changes"] == base["changes"] + fresh
    limits = next(c for c in fresh if c["change"] == "limits_changed")
    assert "50 requests per day" in " ".join(limits["from"]) and limits["to"] == ["25 requests per day (free models) [verified]", "reset: utc_day"]
    policy = next(c for c in fresh if c["change"] == "policy_changed")
    assert (policy["from"], policy["to"]) == ("yes", "no")
    assert pub.problems_of(doc) == []
    # nothing changed: the list is kept exactly, which is what makes --check possible
    again, fresh = pub.build(new, doc, date="2026-11-14")
    assert fresh == [] and again == doc
    # wording-only changes are named without their text
    worded, fresh = pub.build(with_provider(new, "groq", notes="Reworded."), doc)
    assert [(c["change"], c["id"], c["field"], c["from"], c["to"]) for c in fresh] == [("details_changed", "groq", "notes", None, None)]


def test_committed_changes_and_changelog_agree():
    assert {"date": "2026-11-13", "change": "added", "section": "other_allowances", "id": "line-messaging-api", "field": None, "from": None, "to": None} in DOC["changes"]
    log = (OUT / pub.CHANGELOG).read_text(encoding="utf-8")
    for change in DOC["changes"]:
        assert f"## {change['date']}" in log and pub.change_line(change) in log


def test_changelog_is_added_to_never_rewritten(tmp_path):
    first = pub.changelog_with(None, "2026-11-13", [{"date": "2026-11-13", "change": "added", "section": "other_allowances", "id": "a", "field": None, "from": None, "to": None}])
    assert first.startswith("# Catalogue changelog") and "## 2026-11-13\n\n- Added `a` (other allowances)." in first
    second = pub.changelog_with(first + "\nA note someone wrote by hand.\n", "2026-11-20",
                                [{"date": "2026-11-20", "change": "date_changed", "section": "ai_services", "id": "b", "field": "checked_on", "from": "2026-11-12", "to": "2026-11-20"}])
    assert second.index("## 2026-11-20") < second.index("## 2026-11-13") and "A note someone wrote by hand." in second and "- Added `a`" in second
    assert "Date checked changed for `b` (ai services): checked_on 2026-11-12 → 2026-11-20." in second
    third = pub.changelog_with(second, "2026-11-20", [{"date": "2026-11-20", "change": "removed", "section": "ai_services", "id": "c", "field": None, "from": None, "to": None}])
    assert third.count("## 2026-11-20") == 1 and "- Removed `c` (ai services)." in third and "Date checked changed for `b`" in third
    assert pub.changelog_with(third, "2026-11-20", [{"date": "2026-11-20", "change": "removed", "section": "ai_services", "id": "c", "field": None, "from": None, "to": None}]) == third


def test_a_broken_previous_file_is_not_silently_replaced(tmp_path):
    (tmp_path / pub.PRETTY).write_text("{ not json", encoding="utf-8")
    lines: list[str] = []
    assert pub.run(out_dir=tmp_path, say=lines.append) == 2 and "cannot be read" in lines[0]
    assert (tmp_path / pub.PRETTY).read_text(encoding="utf-8") == "{ not json"


# ---------------------------------------------------------------------- CLI


def test_cli_export_and_show(tmp_path, capsys):
    assert cli.main(["catalog", "export", "--check"]) == 0
    assert "catalogue ok" in capsys.readouterr().out
    assert cli.main(["catalog", "export", "--out", str(tmp_path)]) == 0
    capsys.readouterr()
    written = json.loads((tmp_path / "catalogue.v1.json").read_text(encoding="utf-8"))
    assert written["ai_services"] == DOC["ai_services"] and written["changes"] == []  # a fresh folder has no earlier version
    assert cli.main(["catalog", "export", "--out", str(tmp_path), "--check"]) == 0
    capsys.readouterr()

    assert cli.main(["catalog", "show", "line-messaging-api", "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry == next(e for e in DOC["other_allowances"] if e["id"] == "line-messaging-api")
    assert cli.main(["catalog", "show", "openrouter", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "OpenRouter" in out and "requests per day [verified]" in out and "(an estimate)" in out and "OPENROUTER_API_KEY" in out and "source: https://" in out
    assert cli.main(["catalog", "show", "line-messaging-api", "--lang", "zh-TW"]) == 0
    assert "LINE 官方帳號訊息" in capsys.readouterr().out
    assert cli.main(["catalog", "show", "gemini"]) == 0
    assert "allowance: not confirmed" in capsys.readouterr().out
    assert cli.main(["catalog", "show", "no-such-entry"]) == 1
    assert "no entry called" in capsys.readouterr().err
