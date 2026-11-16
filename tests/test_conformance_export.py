"""The conformance vectors in core/conformance are what Python returns today.

core/ (Rust) replays those files in `cargo test`. If a change to muse.quota,
muse.ration or muse.cache changes an answer, this test fails until the files
are regenerated with `python scripts/export_conformance.py`, and then the Rust
tests show what the core has to learn.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMITTED = ROOT / "core" / "conformance"
# permissions.json replays fixed tokens (they cannot be reproduced byte for byte) and needs the
# optional biscuit-python to recompute; without it the exporter passes the committed file through,
# so these tests still hold. tests/test_permissions.py checks the recomputation when it is installed.
AREAS = ("cache.json", "permissions.json", "quota.json", "ration.json", "records.json")


def load_exporter():
    spec = importlib.util.spec_from_file_location("export_conformance", ROOT / "scripts" / "export_conformance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return load_exporter()


@pytest.fixture(scope="module")
def fresh(exporter, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("conformance")
    exporter.write(out)
    return out


def test_committed_vectors_are_up_to_date(fresh):
    assert sorted(p.name for p in fresh.glob("*.json")) == list(AREAS)
    assert sorted(p.name for p in COMMITTED.glob("*.json")) == list(AREAS), "unexpected or missing files in core/conformance"
    for name in AREAS:
        assert (COMMITTED / name).read_bytes() == (fresh / name).read_bytes(), (
            f"core/conformance/{name} is out of date: run `python scripts/export_conformance.py`, "
            "then `cargo test` in core/"
        )


def test_export_is_deterministic(exporter, fresh, tmp_path):
    exporter.write(tmp_path)
    for name in AREAS:
        assert (tmp_path / name).read_bytes() == (fresh / name).read_bytes()


def test_check_mode_reports_stale_files(exporter, fresh, tmp_path):
    assert exporter.stale(fresh) == []
    assert exporter.main(["--check", "--out", str(fresh)]) == 0
    for name in AREAS:
        (tmp_path / name).write_bytes((fresh / name).read_bytes())
    (tmp_path / "quota.json").write_text("[]\n", encoding="utf-8")
    (tmp_path / "extra.json").write_text("[]\n", encoding="utf-8")
    assert exporter.stale(tmp_path) == ["extra.json", "quota.json"]
    assert exporter.main(["--check", "--out", str(tmp_path)]) == 1
    assert exporter.stale(tmp_path / "missing") == list(AREAS)


@pytest.mark.parametrize("name", AREAS)
def test_vector_files_are_well_formed(fresh, name):
    raw = (fresh / name).read_text(encoding="utf-8")
    assert raw.isascii() and raw.endswith("]\n")
    vectors = json.loads(raw, parse_constant=lambda token: pytest.fail(f"bare {token} in {name}"))
    assert len(vectors) > 100
    names = [v["name"] for v in vectors]
    assert len(set(names)) == len(names)
    for vector in vectors:
        assert set(vector) == {"name", "fn", "input", "expected"}
        assert vector["name"].startswith(vector["fn"] + ": ")
        assert isinstance(vector["input"], dict)
        assert set(vector["expected"]) in ({"ok"}, {"err"})


def test_every_ported_function_has_vectors(fresh):
    wanted = {
        "quota.json": {"parse_retry_after", "parse_duration", "parse_reset", "next_midnight_pacific", "next_midnight_utc",
                       "next_month_utc", "month_start", "day_start", "iso", "classify_failure"},
        "ration.json": {"previous_midnight_pacific", "window_bounds", "window_reset", "burn_rate", "pace_position",
                        "pace_release_at", "pace_holds", "options_trailer", "parse_options"},
        "cache.json": {"cacheable", "cache_key"},
        "records.json": set(__import__("muse.records.vectors", fromlist=["FUNCTIONS"]).FUNCTIONS),
    }
    for name, functions in wanted.items():
        assert {v["fn"] for v in json.loads((fresh / name).read_text(encoding="utf-8"))} == functions


def test_vectors_agree_with_the_existing_tables(fresh):
    """Spot checks that the files hold real answers, read the way the Rust test reads them."""
    quota_vectors = {v["name"]: v for v in json.loads((fresh / "quota.json").read_text(encoding="utf-8"))}
    block = quota_vectors["classify_failure: groq TPD hint"]["expected"]["ok"]
    assert (block["scope"], block["kind"]) == ("upstream", "exhausted")
    assert block["until"] == pytest.approx(1794466800.0 + 410.832)
    assert quota_vectors["classify_failure: caller 400"]["expected"] == {"ok": None}
    assert quota_vectors["next_midnight_utc: nan"]["expected"] == {"err": "TimeOutOfRange"}
    cache_vectors = {v["name"]: v for v in json.loads((fresh / "cache.json").read_text(encoding="utf-8"))}
    from muse import cache
    assert cache_vectors["cache_key: base"]["expected"]["ok"] == cache.cache_key(
        "fast", {"messages": [{"role": "user", "content": "hello"}], "temperature": 0}, "standard", "standard")
    assert cache_vectors["cacheable: temperature integer too large for a float"]["expected"] == {"err": "OverflowError"}
