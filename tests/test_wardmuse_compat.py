"""The muse name still works, and the new names win when they are set."""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from wardmuse import envutil
from wardmuse import paths


def test_shim_warns_and_reexports_submodules():
    sys.modules.pop("muse", None)
    sys.modules.pop("muse.paths", None)
    with pytest.warns(DeprecationWarning, match="wardmuse"):
        shim = importlib.import_module("muse")
        compat_paths = importlib.import_module("muse.paths")
    assert compat_paths.home is paths.home
    assert shim.__version__ == importlib.import_module("wardmuse").__version__


def test_python_m_muse_warns_on_import():
    proc = subprocess.run(
        [sys.executable, "-W", "always", "-c", "import muse.cli"],
        cwd=paths.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "wardmuse" in proc.stderr


def test_console_scripts_point_at_both_names():
    text = (paths.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'wardmuse = "wardmuse.cli:main"' in text
    assert 'muse = "muse.cli:main"' in text


def test_new_home_env_wins(tmp_path: Path):
    new = tmp_path / "new"
    old = tmp_path / "old"
    got = paths.home({"WARDMUSE_HOME": str(new), "MUSE_HOME": str(old)})
    assert got == new
    assert new.is_dir() and not old.exists()


def test_old_home_env_is_the_fallback(tmp_path: Path):
    old = tmp_path / "old"
    got = paths.home({"MUSE_HOME": str(old)})
    assert got == old


def test_default_home_is_wardmuse_until_an_old_directory_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
    created = paths.home({})
    assert created == tmp_path / ".wardmuse"
    created.rmdir()
    (tmp_path / ".muse").mkdir()
    assert paths.home({}) == tmp_path / ".muse"


def test_blank_new_env_does_not_hide_the_old_one():
    assert envutil.envget({"WARDMUSE_HOME": "  ", "MUSE_HOME": "/old"}, "HOME") == "/old"
    assert envutil.envget({"WARDMUSE_BIND": "0.0.0.0", "MUSE_BIND": "127.0.0.1"}, "MUSE_BIND") == "0.0.0.0"


def test_render_config_uses_the_same_home_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import render_config

    monkeypatch.setattr(render_config.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".muse").mkdir()
    assert render_config.local_catalog_path({}) == tmp_path / ".muse" / "local.yaml"
    assert render_config.local_catalog_path({"WARDMUSE_HOME": str(tmp_path / "w")}) == tmp_path / "w" / "local.yaml"
    assert render_config.envget({"MUSE_BIND": "127.0.0.1"}, "BIND") == "127.0.0.1"


def test_shell_path_helpers_prefer_new_and_fall_back(tmp_path: Path):
    lib = paths.REPO_ROOT / "deploy" / "vm" / "lib.sh"
    script = f'''
set -euo pipefail
. "{lib}"
test "$(wm_env HOME)" = "/from-new"
export WARDMUSE_HOME=
test "$(wm_env HOME)" = "/from-old"
unset WARDMUSE_HOME MUSE_HOME
mkdir -p "{tmp_path}/.muse"
test "$(HOME="{tmp_path}" wm_dir HOME "{tmp_path}/.wardmuse" "{tmp_path}/.muse")" = "{tmp_path}/.muse"
mkdir -p "{tmp_path}/.wardmuse"
test "$(wm_dir HOME "{tmp_path}/.wardmuse" "{tmp_path}/.muse")" = "{tmp_path}/.wardmuse"
'''
    # wm_env reads the process environment. Seed the old name, then the script sets the new one.
    env = {
        "MUSE_HOME": "/from-old",
        "PATH": "/usr/bin:/bin",
        "BASH_ENV": "",
    }
    # The script's first wm_env expects WARDMUSE_HOME. Set it here and let the script clear it.
    env["WARDMUSE_HOME"] = "/from-new"
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_migration_symlinks_old_paths_without_touching_secrets(tmp_path: Path):
    root = tmp_path / "sys"
    for rel in ("opt/muse/current", "etc/muse", "var/lib/muse/home"):
        (root / rel).mkdir(parents=True)
    secret = root / "etc" / "muse" / "muse.env"
    secret.write_text("LITELLM_MASTER_KEY=sk-keep-me\nMUSE_BIND=127.0.0.1\n", encoding="utf-8")
    before = secret.read_bytes()
    script = paths.REPO_ROOT / "deploy" / "vm" / "migrate-to-wardmuse.sh"
    dry = subprocess.run(["bash", str(script), "--dry-run", "--root", str(root)], capture_output=True, text=True, check=False)
    assert dry.returncode == 0, dry.stderr
    assert "dry run" in dry.stdout and "sk-keep-me" not in dry.stdout + dry.stderr
    assert not (root / "opt" / "wardmuse").exists()
    done = subprocess.run(["bash", str(script), "--yes", "--root", str(root)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert (root / "opt" / "wardmuse").is_symlink()
    assert (root / "etc" / "wardmuse").is_symlink()
    assert (root / "var" / "lib" / "wardmuse").is_symlink()
    assert secret.read_bytes() == before
    assert "muse-stack-litellm-1" in done.stdout and "wardmuse-litellm-1" in done.stdout
