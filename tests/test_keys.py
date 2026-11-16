"""muse.keys: storage, precedence, and that secrets never leak."""

from __future__ import annotations

import os
import stat
import sys
import types

import pytest

from wardmuse import keys

SECRET = "gsk_SUPERSECRETVALUE1234567890"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    for name in list(keys.KNOWN_KEYS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)  # no real keychain by default
    return tmp_path


def fake_keyring(monkeypatch, priority=5):
    store: dict[tuple[str, str], str] = {}
    mod = types.ModuleType("keyring")
    mod.get_keyring = lambda: types.SimpleNamespace(priority=priority)
    mod.get_password = lambda svc, name: store.get((svc, name))
    mod.set_password = lambda svc, name, val: store.__setitem__((svc, name), val)

    def delete_password(svc, name):
        store.pop((svc, name))

    mod.delete_password = delete_password
    monkeypatch.setitem(sys.modules, "keyring", mod)
    return store


def env_path(tmp_path):
    return tmp_path / ".env"


def test_known_keys_come_from_catalogue():
    assert "GROQ_API_KEY" in keys.KNOWN_KEYS
    assert keys.MASTER_KEY in keys.KNOWN_KEYS
    assert keys.KNOWN_KEYS["GROQ_API_KEY"]["provider"] == "groq"
    assert keys.KNOWN_KEYS["GROQ_API_KEY"]["key_url"].startswith("https://")


def test_dotenv_roundtrip_preserves_comments_and_order(tmp_path):
    p = env_path(tmp_path)
    p.write_text("# top comment\nFOO=bar\n\n# groq\nGROQ_API_KEY=\nZED=1\n")
    keys.set_key("GROQ_API_KEY", SECRET)
    text = p.read_text()
    assert text == f"# top comment\nFOO=bar\n\n# groq\nGROQ_API_KEY={SECRET}\nZED=1\n"
    keys.set_key("GEMINI_API_KEY", "AIzaXYZ")
    assert p.read_text().splitlines()[-1] == "GEMINI_API_KEY=AIzaXYZ"
    assert keys.delete_key("GROQ_API_KEY") == ["dotenv"]
    assert "GROQ_API_KEY" not in p.read_text()
    assert "# groq" in p.read_text() and "ZED=1" in p.read_text()


def test_file_is_created_0600(tmp_path):
    keys.set_key("GROQ_API_KEY", SECRET)
    assert stat.S_IMODE(env_path(tmp_path).stat().st_mode) == 0o600


def test_existing_loose_permissions_warn_and_are_tightened(tmp_path):
    p = env_path(tmp_path)
    p.write_text("A=1\n")
    p.chmod(0o644)
    result = keys.set_key("GROQ_API_KEY", SECRET)
    assert any("readable by other users" in w for w in result.warnings)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_write_is_atomic_via_replace(tmp_path, monkeypatch):
    p = env_path(tmp_path)
    p.write_text("KEEP=me\n")
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real(a, b))[1])
    keys.set_key("GROQ_API_KEY", SECRET)
    assert calls and calls[0][1] == p and calls[0][0] != p
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def test_failed_write_leaves_original_and_no_temp(tmp_path, monkeypatch):
    p = env_path(tmp_path)
    p.write_text("KEEP=me\n")

    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        keys.set_key("GROQ_API_KEY", SECRET)
    assert p.read_text() == "KEEP=me\n"
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def test_symlink_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("X=1\n")
    env_path(tmp_path).symlink_to(target)
    with pytest.raises(keys.KeysError, match="symlink"):
        keys.set_key("GROQ_API_KEY", SECRET)
    assert target.read_text() == "X=1\n"


def test_precedence_env_then_keychain_then_dotenv(tmp_path, monkeypatch):
    store = fake_keyring(monkeypatch)
    env_path(tmp_path).write_text("GROQ_API_KEY=from-dotenv\n")
    assert keys.source_of("GROQ_API_KEY") == "dotenv"
    store[(keys.SERVICE, "GROQ_API_KEY")] = "from-keychain"
    assert keys.source_of("GROQ_API_KEY") == "keychain"
    assert keys.export_env({})["GROQ_API_KEY"] == "from-keychain"
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert keys.source_of("GROQ_API_KEY") == "env"
    assert keys.export_env()["GROQ_API_KEY"] == "from-env"
    assert keys.is_configured("GROQ_API_KEY") is True


def test_is_configured_accepts_explicit_environ():
    assert keys.is_configured("GROQ_API_KEY", {"GROQ_API_KEY": "x"}) is True
    assert keys.is_configured("GROQ_API_KEY", {"GROQ_API_KEY": "  "}) is False


def test_auto_backend_prefers_keychain_when_usable(tmp_path, monkeypatch):
    store = fake_keyring(monkeypatch)
    res = keys.set_key("GROQ_API_KEY", SECRET)
    assert res.backend == "keychain"
    assert store[(keys.SERVICE, "GROQ_API_KEY")] == SECRET
    assert not env_path(tmp_path).exists()
    assert keys.delete_key("GROQ_API_KEY") == ["keychain"]


def test_unusable_keyring_backend_falls_back_to_dotenv(tmp_path, monkeypatch):
    fake_keyring(monkeypatch, priority=0)  # keyring's "fail" backend
    assert keys.keychain_available() is False
    assert keys.set_key("GROQ_API_KEY", SECRET).backend == "dotenv"


def test_keyring_absent_degrades_cleanly(tmp_path):
    assert keys.keychain_available() is False
    with pytest.raises(keys.KeysError, match="keychain"):
        keys.set_key("GROQ_API_KEY", SECRET, backend="keychain")
    assert keys.set_key("GROQ_API_KEY", SECRET).backend == "dotenv"


def test_list_keys_has_no_values(tmp_path, monkeypatch):
    keys.set_key("GROQ_API_KEY", SECRET)
    rows = keys.list_keys()
    groq = next(r for r in rows if r["name"] == "GROQ_API_KEY")
    assert groq == {"name": "GROQ_API_KEY", "provider": "groq", "configured": True, "source": "dotenv", "key_url": groq["key_url"]}
    assert SECRET not in repr(rows)
    assert next(r for r in rows if r["name"] == "GEMINI_API_KEY")["source"] is None


def test_unknown_name_needs_force():
    with pytest.raises(keys.KeysError, match="not a key"):
        keys.set_key("MYSTERY_TOKEN", "abcdefghijklmnop")
    assert keys.set_key("MYSTERY_TOKEN", "abcdefghijklmnop", force=True).backend == "dotenv"
    with pytest.raises(keys.KeysError):
        keys.validate_name("lower-case", force=True)


@pytest.mark.parametrize(
    "raw,expected",
    [("  abc123\n", "abc123"), ('"abc123"', "abc123"), ("'abc123'\r\n", "abc123"), (' " abc123 " ', "abc123")],
)
def test_clean_value(raw, expected):
    assert keys.clean_value(raw) == expected


@pytest.mark.parametrize("raw", ["", "   \n", "abc\ndef", '"abc\r\ndef"'])
def test_clean_value_rejects(raw):
    with pytest.raises(keys.KeysError):
        keys.clean_value(raw)


def test_shape_hints_warn_but_do_not_block(tmp_path):
    res = keys.set_key("GROQ_API_KEY", "not-a-groq-key-1234567")
    assert any("gsk_" in w for w in res.warnings)
    assert "not-a-groq-key" not in " ".join(res.warnings)
    assert keys.is_configured("GROQ_API_KEY")
    assert keys.shape_hint("GEMINI_API_KEY", "AIzaSyFoo") is None
    assert "AIza" in keys.shape_hint("GEMINI_API_KEY", "nope")


def test_quoting_for_awkward_values(tmp_path):
    keys.set_key("GROQ_API_KEY", "has space#x")
    assert 'GROQ_API_KEY="has space#x"' in env_path(tmp_path).read_text()
    assert keys.export_env()["GROQ_API_KEY"] == "has space#x"


def test_master_key_generation():
    a, b = keys.generate_master_key(), keys.generate_master_key()
    assert a.startswith("sk-muse-") and a != b and len(a) >= len("sk-muse-") + 43
    assert not keys.is_weak_master_key(a)
    assert keys.is_weak_master_key("sk-1234")


def test_duplicate_and_variant_detection(tmp_path, monkeypatch):
    env_path(tmp_path).write_text("GROQ_API_KEY=same-value-1234\nJINA_API_KEY=same-value-1234\nGROQ_API_KEY_2=other\n")
    dupes = keys.find_duplicate_values()
    assert dupes == [["GROQ_API_KEY", "JINA_API_KEY"]]
    assert "same-value-1234" not in repr(dupes)
    assert keys.extra_key_variants() == ["GROQ_API_KEY_2"]


def test_no_value_in_reprs_exceptions_or_output(tmp_path, monkeypatch, capsys, caplog):
    store = fake_keyring(monkeypatch)
    caplog.set_level("DEBUG")
    res = keys.set_key("GROQ_API_KEY", SECRET)
    assert SECRET not in repr(res) and SECRET not in str(res)
    keys.set_key("GEMINI_API_KEY", SECRET, backend="dotenv")
    keys.list_keys()
    keys.find_duplicate_values()
    keys.delete_key("GROQ_API_KEY")

    # a keychain that fails with the value in its own message must not leak it
    def bad_set(svc, name, val):
        raise RuntimeError(f"cannot store {val}")

    sys.modules["keyring"].set_password = bad_set
    with pytest.raises(keys.KeysError) as ei:
        keys.set_key("GROQ_API_KEY", SECRET)
    assert SECRET not in str(ei.value) and ei.value.__cause__ is None

    with pytest.raises(keys.KeysError) as ei2:
        keys.clean_value(f"{SECRET}\nsecond line")
    assert SECRET not in str(ei2.value)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert SECRET not in caplog.text
    assert store == {}  # keychain entry for GROQ_API_KEY was deleted above
