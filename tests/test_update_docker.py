"""Docker-mode updates: signature, downgrade, checksum, lock, rollback, migration.

No Docker daemon and no network. curl and the docker CLI are stand-ins that
record what they were asked to do. The signing key in this file is generated
per test and never written into the repository.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / "deploy" / "vm"
SCRIPTS = ROOT / "scripts"
SECRET = "sk-test-secret-value"
TOKEN = "secret-token-value"
NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def load_script(name: str, path: Path):
    import importlib.machinery
    import importlib.util

    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def run(cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    full = os.environ.copy()
    full.update(env)
    return subprocess.run(cmd, env=full, cwd=cwd, capture_output=True, text=True, timeout=60)


def ledger(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(row,) for row in rows])
    conn.commit()
    conn.close()


def ledger_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY id")]
    finally:
        conn.close()


def pack(root: Path, dest: Path, version: str) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            tar.add(path, arcname=f"muse-{version}/{path.relative_to(root).as_posix()}")
    payload = gzip.compress(raw.getvalue(), mtime=0)
    dest.write_bytes(payload)
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def write_smoke(directory: Path, body: str) -> None:
    scripts = directory / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "smoke_test.py").write_text(body, encoding="utf-8")
    (directory / "docker-compose.yml").write_text("services:\n  litellm:\n    image: example.invalid/none:pinned\n", encoding="utf-8")


SMOKE_OK = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
Path(os.environ["MUSE_HOME"], "smoke.log").write_text("ok\\n", encoding="utf-8")
sys.exit(0)
"""

SMOKE_BAD = """#!/usr/bin/env python3
import os, sqlite3, sys
home = os.environ["MUSE_HOME"]
conn = sqlite3.connect(home + "/ledger.sqlite")
conn.execute("INSERT INTO t (v) VALUES ('from-bad-release')")
conn.commit()
sys.exit(1)
"""


class World:
    def __init__(self, tmp: Path) -> None:
        pytest.importorskip("cryptography")
        self.tmp = tmp
        self.base = tmp / "opt" / "muse"
        self.home = tmp / "home"
        self.state = tmp / "vm"
        self.env_file = tmp / "muse.env"
        self.env_file.write_text(f"LITELLM_MASTER_KEY={SECRET}\n", encoding="utf-8")
        os.chmod(self.env_file, 0o600)
        self.curl_root = tmp / "curl"
        self.curl_root.mkdir()
        self.docker_log = tmp / "docker.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(
            """#!/bin/bash
out=""
url=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--output" ]; then out="$arg"; fi
  case "$arg" in
    http://*|https://*) url="$arg" ;;
  esac
  prev="$arg"
done
root="${MUSE_FAKE_CURL_ROOT:?}"
case "$url" in
  https://updates.example/stable.json) cp "$root/manifest.json" "$out" ;;
  https://updates.example/stable.json.sig) cp "$root/manifest.json.sig" "$out" ;;
  https://updates.example/*.tar.gz) cp "$root/release.tar.gz" "$out" ;;
  http://127.0.0.1:*/health/liveliness) exit 0 ;;
  https://hc.example/*) printf '%s\\n' "$url" >> "$root/pings.log"; exit 0 ;;
  *) printf '%s\\n' "$url" >> "$root/unexpected.log"; exit 1 ;;
esac
""",
            encoding="utf-8",
        )
        (self.bin / "docker").write_text(
            """#!/bin/bash
printf '%s\\n' "$*" >> "${MUSE_FAKE_DOCKER_LOG:?}"
case "$*" in
  *validate_config.py*) printf 'config ok\\n' ;;
esac
exit 0
""",
            encoding="utf-8",
        )
        os.chmod(self.bin / "curl", 0o755)
        os.chmod(self.bin / "docker", 0o755)
        self.sign = load_script("sign_manifest_under_test", SCRIPTS / "sign_manifest.py")
        self.verify = load_script("update_verify_under_test", VM / "update_verify.py")
        seed = os.urandom(32)
        self.public = self.sign.public_from_seed(seed)
        self.seed = seed
        ledger(self.home / "ledger.sqlite", ["kept"])
        self._release("0.2.0", SMOKE_OK)
        self._release("0.1.0", SMOKE_OK)
        (self.base / "current").symlink_to("releases/0.2.0")
        self.hc = tmp / "healthchecks.url"
        self.hc.write_text(f"https://hc.example/ping/{TOKEN}\n", encoding="utf-8")

    def _release(self, version: str, smoke: str) -> None:
        directory = self.base / "releases" / version
        write_smoke(directory, smoke)

    def publish(self, version: str, *, sha: str | None = None, expires: str | None = None, smoke: str = SMOKE_OK) -> None:
        src = self.tmp / f"src-{version}"
        if src.exists():
            import shutil
            shutil.rmtree(src)
        write_smoke(src, smoke)
        archive = self.curl_root / "release.tar.gz"
        digest = pack(src, archive, version)
        manifest = self.sign.build_manifest(
            version=version,
            url=f"https://updates.example/muse-{version}.tar.gz",
            sha256=sha or digest,
            expires=expires or self.sign.expires_in(30),
            commit="a" * 40,
        )
        (self.curl_root / "manifest.json").write_bytes(manifest)
        (self.curl_root / "manifest.json.sig").write_bytes(self.sign.sign_bytes(manifest, self.seed))

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "MUSE_VM_CONF": "/nonexistent/vm.conf",
            "MUSE_BASE": str(self.base),
            "MUSE_VM_STATE": str(self.state),
            "MUSE_HOME": str(self.home),
            "MUSE_ENV_FILE": str(self.env_file),
            "MUSE_RUNTIME": "docker",
            "MUSE_UPDATE_MANIFEST_URL": "https://updates.example/stable.json",
            "MUSE_UPDATE_PUBKEY": self.public,
            "MUSE_UPDATE_PYTHON": sys.executable,
            "MUSE_DOCKER": str(self.bin / "docker"),
            "MUSE_COMPOSE_PROJECT": "muse-stack",
            "MUSE_HEALTHCHECKS_URL_FILE": str(self.hc),
            "MUSE_FAKE_CURL_ROOT": str(self.curl_root),
            "MUSE_FAKE_DOCKER_LOG": str(self.docker_log),
            "MUSE_UPDATE_WAIT": "6",
            "MUSE_USER": "nobody-such-user",
            "HOME": str(self.tmp / "nobody-home"),
        }
        base.update(extra)
        return base

    def update(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return run(["bash", str(VM / "muse-update.sh"), *args], self.env(**extra))

    def current(self) -> str:
        return (self.base / "current").resolve().name

    def last(self) -> dict:
        return json.loads((self.state / "update" / "last.json").read_text(encoding="utf-8"))

    def pings(self) -> str:
        path = self.curl_root / "pings.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def docker(self) -> str:
        return self.docker_log.read_text(encoding="utf-8") if self.docker_log.exists() else ""

    def leaked(self, result: subprocess.CompletedProcess) -> bool:
        blob = result.stdout + result.stderr
        last = (self.state / "update" / "last.json").read_text(encoding="utf-8") if (self.state / "update" / "last.json").exists() else ""
        return SECRET in blob or TOKEN in blob or SECRET in last or TOKEN in last


def test_signature_downgrade_checksum_and_lock(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.publish("0.2.0")
    same = world.update()
    assert same.returncode == 0, same.stderr
    assert world.last()["result"] == "up-to-date"
    assert world.current() == "0.2.0" and world.docker() == ""
    assert "https://hc.example/ping/" in world.pings() and "/fail" not in world.pings() and "/start" not in world.pings()
    assert not world.leaked(same)

    world.publish("0.1.0")
    older = world.update()
    assert older.returncode != 0 and world.last()["result"] == "refused"
    assert "older" in world.last()["detail"] or "downgrade" in world.last()["detail"]
    assert world.current() == "0.2.0" and world.docker() == ""
    assert "/fail" in world.pings()
    assert not world.leaked(older)

    world.publish("0.3.0", expires="2020-01-01T00:00:00Z")
    expired = world.update()
    assert expired.returncode != 0 and world.last()["result"] == "refused"
    assert "expired" in world.last()["detail"]
    assert world.docker() == ""

    world.publish("0.3.0")
    sig = world.curl_root / "manifest.json.sig"
    raw = bytearray(base64.b64decode(sig.read_text().strip()))
    raw[0] ^= 0x01
    sig.write_text(base64.b64encode(bytes(raw)).decode() + "\n", encoding="utf-8")
    bad = world.update()
    assert bad.returncode != 0 and world.last()["result"] == "refused"
    assert "signature" in world.last()["detail"]
    assert world.current() == "0.2.0" and "0.3.0" not in world.docker()

    world.publish("0.3.0", sha="b" * 64)
    mismatch = world.update()
    assert mismatch.returncode != 0 and world.last()["result"] == "refused"
    assert "SHA-256" in world.last()["detail"]
    assert not (world.base / "releases" / "0.3.0").exists()
    assert world.docker() == ""
    assert not world.leaked(mismatch)

    import fcntl
    lock_dir = world.state / "update"
    lock_dir.mkdir(parents=True, exist_ok=True)
    sentinel = lock_dir / "last.json"
    sentinel.write_text('{"result":"sentinel"}\n', encoding="utf-8")
    fd = os.open(lock_dir / "muse-update.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = world.update()
    finally:
        os.close(fd)
    assert busy.returncode == 0 and "another update is already running" in busy.stdout
    assert sentinel.read_text(encoding="utf-8") == '{"result":"sentinel"}\n'
    assert world.current() == "0.2.0"


def test_update_switches_and_rolls_back_when_smoke_fails(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.publish("0.3.0", smoke=SMOKE_OK)
    ok = world.update()
    assert ok.returncode == 0, ok.stderr
    assert world.last()["result"] == "updated" and world.current() == "0.3.0"
    assert ledger_rows(world.home / "ledger.sqlite") == ["kept"]
    assert (world.home / "smoke.log").read_text(encoding="utf-8") == "ok\n"
    link = world.base / "releases" / "0.3.0" / ".env"
    assert link.is_symlink() and link.resolve() == world.env_file.resolve()
    assert not (world.base / "releases" / "0.1.0").exists()
    assert (world.base / "releases" / "0.2.0").is_dir()
    assert (world.state / "update" / "rollback" / "previous").read_text(encoding="utf-8").strip() == "0.2.0"
    log = world.docker()
    assert "pull" in log and "validate_config.py" in log and "up -d" in log
    assert "/start" in world.pings() and "/fail" not in world.pings()
    assert not world.leaked(ok)
    assert world.env_file.read_text(encoding="utf-8") == f"LITELLM_MASTER_KEY={SECRET}\n"

    conn = sqlite3.connect(world.home / "ledger.sqlite")
    conn.execute("INSERT INTO t (v) VALUES ('after-success')")
    conn.commit()
    conn.close()
    back = world.update("--rollback")
    assert back.returncode != 0, back.stderr
    assert world.last()["result"] == "rolled-back" and world.current() == "0.2.0"
    assert ledger_rows(world.home / "ledger.sqlite") == ["kept"]
    assert "MUSE_UPDATE_PIN" in world.last()["detail"]
    assert (world.base / "releases" / "0.3.0").is_dir()
    assert not world.leaked(back)

    world.docker_log.unlink()
    pings = world.curl_root / "pings.log"
    if pings.exists():
        pings.unlink()
    world.publish("0.4.0", smoke=SMOKE_BAD)
    # current is 0.2.0, so 0.4.0 is newer. Put current back on 0.3.0 first? 0.4.0 > 0.2.0.
    failed = world.update()
    assert failed.returncode != 0, failed.stderr
    assert world.last()["result"] == "rolled-back" and world.current() == "0.2.0"
    assert ledger_rows(world.home / "ledger.sqlite") == ["kept"]
    assert "from-bad-release" not in " ".join(ledger_rows(world.home / "ledger.sqlite"))
    assert not (world.base / "releases" / "0.4.0").exists()
    rolled = world.docker()
    assert "releases/0.4.0" in rolled and "releases/0.2.0" in rolled
    assert "/fail" in world.pings()
    assert not world.leaked(failed)


def test_interrupted_update_rolls_back_before_fetching(tmp_path: Path) -> None:
    world = World(tmp_path)
    snap = load_script("update_snapshot_pending", VM / "update_snapshot.py")
    pending_tar = world.state / "update" / "snapshots" / "pending.tar"
    snap.snapshot(world.home, pending_tar)
    conn = sqlite3.connect(world.home / "ledger.sqlite")
    conn.execute("INSERT INTO t (v) VALUES ('half-applied')")
    conn.commit()
    conn.close()
    pending = world.state / "update" / "pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({
        "previous": "0.2.0",
        "target": "0.3.0",
        "snapshot": str(pending_tar),
    }), encoding="utf-8")
    world.publish("0.9.0")
    result = world.update()
    assert result.returncode != 0, result.stderr
    assert world.last()["result"] == "rolled-back"
    assert world.current() == "0.2.0"
    assert ledger_rows(world.home / "ledger.sqlite") == ["kept"]
    assert "pull" not in world.docker()
    assert not world.leaked(result)


def test_pin_refuses_a_newer_release(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.publish("0.3.0")
    result = world.update(**{"MUSE_UPDATE_PIN": "0.2.0"})
    assert result.returncode == 0, result.stderr
    assert world.last()["result"] == "pinned" and world.current() == "0.2.0"
    assert world.docker() == ""
    assert "/fail" not in world.pings()
    assert not world.leaked(result)


def test_snapshot_round_trip_and_rejects_escape(tmp_path: Path) -> None:
    tool = load_script("update_snapshot_round", VM / "update_snapshot.py")
    home = tmp_path / "home"
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    ledger(home / "ledger.sqlite", ["one", "two"])
    conn = sqlite3.connect(home / "ledger.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO t (v) VALUES ('three')")
    conn.commit()
    conn.close()
    (home / "proxy.log").write_text("noise", encoding="utf-8")
    (home / "device.key").write_text("device", encoding="utf-8")
    (home / "link").symlink_to(outside)
    archive = tmp_path / "home.tar"
    tool.snapshot(home, archive)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    conn = sqlite3.connect(home / "ledger.sqlite")
    conn.execute("INSERT INTO t (v) VALUES ('four')")
    conn.commit()
    conn.close()
    (home / "new-file").write_text("gone", encoding="utf-8")
    tool.restore(archive, home)
    assert ledger_rows(home / "ledger.sqlite") == ["one", "two", "three"]
    assert not (home / "new-file").exists() and not (home / "proxy.log").exists()
    assert (home / "device.key").read_text(encoding="utf-8") == "device"
    assert (home / "link").is_symlink() and os.readlink(home / "link") == str(outside)
    assert outside.read_text(encoding="utf-8") == "untouched"

    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as tar:
        payload = b'{"format": 1}\n'
        info = tarfile.TarInfo("snapshot.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        escape = tarfile.TarInfo("../escaped.txt")
        escape.size = 4
        tar.addfile(escape, io.BytesIO(b"nope"))
    with pytest.raises(tool.SnapshotError, match="unsafe path"):
        tool.restore(bad, tmp_path / "restored")
    assert not (tmp_path / "escaped.txt").exists()


def test_sign_manifest_round_trip_and_keeps_the_private_key_quiet(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    sign = load_script("sign_manifest_cli", SCRIPTS / "sign_manifest.py")
    verify = load_script("update_verify_sign", VM / "update_verify.py")
    generated = subprocess.run([sys.executable, str(SCRIPTS / "sign_manifest.py"), "generate", "--dir", str(tmp_path / "keys")],
                               capture_output=True, text=True, timeout=30)
    assert generated.returncode == 0, generated.stderr
    private = (tmp_path / "keys" / "release.key").read_text(encoding="utf-8").strip()
    public = generated.stdout.strip()
    assert public and private not in generated.stdout and private not in generated.stderr
    assert stat.S_IMODE((tmp_path / "keys" / "release.key").stat().st_mode) == 0o600
    again = subprocess.run([sys.executable, str(SCRIPTS / "sign_manifest.py"), "generate", "--dir", str(tmp_path / "keys")],
                           capture_output=True, text=True, timeout=30)
    assert again.returncode != 0 and private not in again.stderr

    expires = sign.expires_in(30, now=NOW)
    url = "https://github.com/zandis/Muse-stack/releases/download/v0.2.0/muse-0.2.0.tar.gz"
    first = sign.build_manifest(version="v0.2.0", url=url, sha256="a" * 64, expires=expires, commit="b" * 40)
    assert first == sign.build_manifest(version="0.2.0", url=url, sha256="a" * 64, expires=expires, commit="b" * 40)
    signature = sign.sign_bytes(first, base64.b64decode(private))
    assert verify.decide(first, signature, public, "0.1.0", now=NOW) == f"newer 0.2.0 {url} {'a' * 64}"
    tampered = bytearray(first)
    tampered[20] ^= 0x01
    with pytest.raises(verify.Refused, match="signature"):
        verify.decide(bytes(tampered), signature, public, "0.1.0", now=NOW)
    loose = tmp_path / "loose.key"
    loose.write_text(private + "\n", encoding="utf-8")
    os.chmod(loose, 0o644)
    (tmp_path / "m.json").write_bytes(first)
    refused = subprocess.run([sys.executable, str(SCRIPTS / "sign_manifest.py"), "sign", "--manifest", str(tmp_path / "m.json"),
                              "--key", str(loose), "--out", str(tmp_path / "m.sig")],
                             capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0 and private not in refused.stdout and private not in refused.stderr
    far = NOW + timedelta(days=120)
    far_manifest = sign.build_manifest(version="0.2.0", url=url, sha256="a" * 64, expires=far.strftime("%Y-%m-%dT%H:%M:%SZ"))
    far_sig = sign.sign_bytes(far_manifest, base64.b64decode(private))
    with pytest.raises(verify.Refused, match="90 days"):
        verify.decide(far_manifest, far_sig, public, "0.1.0", now=NOW)


def test_smoke_only_fast_needs_a_key(tmp_path: Path) -> None:
    smoke = load_script("smoke_test_only", SCRIPTS / "smoke_test.py")
    env = tmp_path / ".env"
    env.write_text("LITELLM_MASTER_KEY=\n", encoding="utf-8")
    assert smoke.main(["--only", "fast", "--env-file", str(env), "--base-url", "http://127.0.0.1:9"]) == 2
    assert smoke.main(["--only", "not-an-alias", "--env-file", str(env)]) == 2


def test_migration_preserves_the_clone_env_and_can_roll_back(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "docker-compose.yml").write_text("services:\n  litellm:\n    image: example.invalid/none:pinned\n", encoding="utf-8")
    (clone / "README.md").write_text("hello\n", encoding="utf-8")
    (clone / ".gitignore").write_text(".env\n", encoding="utf-8")
    secret = b"LITELLM_MASTER_KEY=sk-migrate-secret\nGROQ_API_KEY=gsk-migrate-secret\n"
    (clone / ".env").write_bytes(secret)
    os.chmod(clone / ".env", 0o640)
    git = ["git", "-C", str(clone)]
    subprocess.run([*git, "init"], check=True, capture_output=True)
    subprocess.run([*git, "config", "user.email", "test@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "Test"], check=True)
    subprocess.run([*git, "add", "docker-compose.yml", "README.md", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-m", "clone"], check=True, capture_output=True)
    mode_before = stat.S_IMODE((clone / ".env").stat().st_mode)
    src = tmp_path / "data"
    ledger(src / "ledger.sqlite", ["from-clone"])
    root = tmp_path / "sys"
    user = subprocess.run(["id", "-un"], check=True, capture_output=True, text=True).stdout.strip()
    base_env = {
        "MUSE_MIGRATE_ALLOW_UNPRIV": "1",
        "MUSE_VM_CONF": "/nonexistent/vm.conf",
        "MUSE_UPDATE_PYTHON": sys.executable,
        "HOME": str(tmp_path / "empty-home"),
    }
    cmd = ["bash", str(VM / "migrate-from-clone.sh"), "--clone", str(clone), "--root", str(root),
           "--user", user, "--home", str(src), "--no-start"]
    dry = run([*cmd, "--dry-run"], base_env)
    assert dry.returncode == 0, dry.stderr
    assert "dry run" in dry.stdout and b"sk-migrate-secret" not in dry.stdout.encode() and b"sk-migrate-secret" not in dry.stderr.encode()
    assert not (root / "etc" / "muse" / "muse.env").exists()
    assert (clone / ".env").read_bytes() == secret

    done = run([*cmd, "--yes"], base_env)
    assert done.returncode == 0, done.stderr
    copied = root / "etc" / "muse" / "muse.env"
    assert copied.read_bytes() == secret and stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert (clone / ".env").read_bytes() == secret and stat.S_IMODE((clone / ".env").stat().st_mode) == mode_before
    conf = (root / "etc" / "muse" / "vm.conf").read_text(encoding="utf-8")
    assert "MUSE_RUNTIME=docker\n" in conf and "MUSE_IDLE_GUARD=off\n" in conf
    assert "\nMUSE_UPDATE_PUBKEY=\n" in conf or conf.endswith("MUSE_UPDATE_PUBKEY=\n")
    assert b"sk-migrate-secret" not in conf.encode() and b"sk-migrate-secret" not in done.stdout.encode()
    releases = list((root / "opt" / "muse" / "releases").iterdir())
    assert len(releases) == 1 and releases[0].name.startswith("0.0.0+")
    updater = (releases[0] / "deploy" / "vm" / "muse-update.sh").read_text(encoding="utf-8")
    assert "MUSE_RUNTIME" in updater
    assert (releases[0] / ".env").is_symlink()
    assert (releases[0] / ".env").resolve() == copied.resolve()
    assert not (releases[0] / ".env").is_file() or (releases[0] / ".env").is_symlink()
    assert (root / "opt" / "muse" / "current").resolve() == releases[0].resolve()
    assert ledger_rows(root / "var" / "lib" / "muse" / "home" / "ledger.sqlite") == ["from-clone"]
    assert ledger_rows(src / "ledger.sqlite") == ["from-clone"]
    units = {path.name for path in (root / "etc" / "systemd" / "system").iterdir()}
    assert "muse-update.service" in units and "muse-update.timer" in units and "muse-heartbeat.timer" in units
    assert "muse-proxy.service" not in units
    migration = json.loads((root / "var" / "lib" / "muse" / "vm" / "migration.json").read_text(encoding="utf-8"))
    assert migration["result"] == "ok" and "sk-migrate-secret" not in json.dumps(migration)
    again = run([*cmd, "--yes"], base_env)
    assert again.returncode == 0 and "already migrated" in again.stdout
    assert (clone / ".env").read_bytes() == secret

    back = run(["bash", str(VM / "migrate-from-clone.sh"), "--rollback", "--yes", "--no-start",
                "--clone", str(clone), "--root", str(root)], base_env)
    assert back.returncode == 0, back.stderr
    assert "not run" in back.stdout and str(clone) in back.stdout
    assert (clone / ".env").read_bytes() == secret and copied.read_bytes() == secret
    assert (root / "opt" / "muse" / "current").exists()

    dirty = clone / "README.md"
    dirty.write_text("changed\n", encoding="utf-8")
    refused = run([*cmd, "--dry-run"], base_env)
    assert refused.returncode != 0 and "uncommitted" in refused.stderr


def test_release_workflow_is_github_hosted_and_not_a_pull_request(tmp_path: Path) -> None:
    import yaml
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "pull_request" not in text and "self-hosted" not in text
    doc = yaml.safe_load(text)
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers and "push" in triggers and "pull_request" not in triggers
    assert "v*.*.*" in triggers["push"]["tags"]
    for name, job in doc["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", name
    assert doc["jobs"]["sign"]["environment"] == "release"
    assert "MUSE_SIGN_IN_CI" in doc["jobs"]["sign"]["if"]
    assert "secrets.MUSE_RELEASE_SIGNING_KEY" in text
    assert "environment:" not in text.split("sign:")[0]
