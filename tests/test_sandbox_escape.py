"""Escape attempts by a really hostile skill (tests/sandbox_skills/escape), against each isolation layer by itself.

    python -m pytest tests/test_sandbox_escape.py -q -rs

Every attempt is run with ONE layer switched on (`bwrap`, `landlock` or `seccomp`) and with everything this machine
has (`all`). A test asserts the attempt fails when the layer is active here, and is SKIPPED, with the reason, when
the layer cannot be used on this machine: nothing is faked. `-rs` prints the skip reasons.

Two things keep these tests honest:
  * `test_without_any_layer_the_same_attempts_succeed` runs the same attempts with every layer off and expects them
    to WORK. If an attempt could not succeed at all, "it was blocked" would prove nothing.
  * Wherever an escape leaves a trace outside the sandbox (a file, a received packet, a dead process), the test
    checks the trace, not only what the hostile skill says about itself.

What each layer is expected to stop is the table in docs/sandbox.md. Known gaps are tests too (`test_known_gap_*`).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from test_sandbox_support import (  # noqa: F401
    CANARY_ENV, CANARY_HOME, CANARY_OUTSIDE, LINUX, alive, attempt, box, caps, chat, env, need, proxy,
)

pytestmark = pytest.mark.skipif(not LINUX, reason=f"the isolation layers are Linux-only; this is {sys.platform}. "
                                                  "macOS has an untested profile and Windows has none (docs/sandbox.md).")

FS_LAYERS = ["bwrap", "landlock", "all"]       # layers that confine files
NET_LAYERS = ["bwrap", "seccomp", "all"]       # layers that leave no network at all


def leaked(result, *canaries: str) -> bool:
    text = json.dumps(result.public(), default=str)
    return any(canary in text for canary in canaries)


@pytest.fixture()
def victim():
    """A process the hostile skill will try to kill or read. Its environment holds a canary."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env={"VICTIM_SECRET": CANARY_ENV, "PATH": "/usr/bin:/bin"})
    yield proc
    proc.kill()
    proc.wait()


class Listener:
    """A local TCP, UDP or unix listener that records whether anything ever arrived."""

    def __init__(self, kind: str, path: str = "") -> None:
        self.kind, self.got = kind, []
        if kind == "unix":
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(path)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM if kind == "tcp" else socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
        if kind != "udp":
            self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1] if kind != "unix" else 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                if self.kind == "udp":
                    self.got.append(self.sock.recvfrom(100)[0])
                else:
                    conn, _ = self.sock.accept()
                    conn.settimeout(1)
                    try:
                        self.got.append(conn.recv(100))
                    except OSError:
                        self.got.append(b"(connected)")
                    conn.close()
            except OSError:
                continue

    def close(self) -> list:
        time.sleep(0.3)
        self._stop = True
        self.thread.join(2)
        self.sock.close()
        return self.got


# ================================================================ the control: with no layer, the attempts work


def test_without_any_layer_the_same_attempts_succeed(box, proxy, victim):
    """Proof that the attempts are real: with every layer off, a hostile skill does all of this."""
    out, result, report = attempt(box, "read", "none", path=str(box.outside / "passwd"))
    assert out["escaped"] and out["detail"] == CANARY_OUTSIDE and not report.isolated and report.files == "not isolated"
    assert attempt(box, "read", "none", path=str(box.home / "canary.txt"))[0]["detail"] == CANARY_HOME
    assert attempt(box, "write", "none", path=str(box.outside / "planted.txt"))[0]["escaped"] and (box.outside / "planted.txt").exists()
    if os.geteuid() != 0:  # as root the child has dropped its capabilities, which already denies this even with no layer
        assert CANARY_ENV in attempt(box, "read", "none", path=f"/proc/{victim.pid}/environ")[0]["detail"]
    assert attempt(box, "shell", "none", command="echo ran")[0]["detail"] == {"code": 0, "out": "ran\n"}
    tcp = Listener("tcp")
    assert attempt(box, "tcp", "none", host="127.0.0.1", port=tcp.port)[0]["escaped"] and tcp.close()
    udp = Listener("udp")
    assert attempt(box, "udp", "none", host="127.0.0.1", port=udp.port)[0]["escaped"] and udp.close()
    assert attempt(box, "signal", "none", pid=victim.pid)[0]["escaped"]
    victim.wait(5)
    assert victim.returncode == -9  # the victim really died


# ================================================================ files


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_cannot_read_a_file_outside_the_named_folders(box, proxy, layer):
    need(layer)
    out, result, report = attempt(box, "read", layer, path=str(box.outside / "passwd"))
    assert out["escaped"] is False and out["error"] in ("PermissionError", "FileNotFoundError"), out
    assert not leaked(result, CANARY_OUTSIDE) and report.files == "isolated"
    out, result, _ = attempt(box, "read", layer, path="/etc/passwd")
    assert out["escaped"] is False, out
    out, result, _ = attempt(box, "list", layer, path=str(box.outside))
    assert out["escaped"] is False, out


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_cannot_read_muse_home(box, proxy, layer):
    need(layer)
    (box.tmp / ".env").write_text(f"GROQ_API_KEY={CANARY_ENV}\n", encoding="utf-8")  # the key file of this test home (MUSE_ENV_FILE)
    for path in (box.home / "canary.txt", box.home / "apps.json", box.home / "skills-approved.json", box.tmp / ".env"):
        out, result, _ = attempt(box, "read", layer, path=str(path))
        assert out["escaped"] is False and not leaked(result, CANARY_HOME, CANARY_ENV), (path, out)
    out, result, _ = attempt(box, "list", layer, path=str(box.home))
    assert out["escaped"] is False or out["detail"] == ["skills-data"], out  # under bubblewrap only the way to its own data folder exists
    assert not leaked(result, "canary.txt")


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_can_still_use_what_it_was_given(box, proxy, layer):
    """The other side of the wall: the named folder is readable and the data folder is writable."""
    need(layer)
    assert attempt(box, "read", layer, path=str(box.docs / "a.txt"))[0] == {"escaped": True, "error": "", "detail": "alpha beta gamma\n"}
    assert attempt(box, "write", layer, path=str(box.data / "own.txt"))[0]["escaped"] is True
    assert (box.data / "own.txt").read_bytes() == b"escaped"


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_cannot_write_outside_its_data_folder(box, proxy, layer):
    need(layer)
    before = (box.docs / "a.txt").read_text()
    for path in (box.outside / "planted.txt", box.docs / "planted.txt", box.home / "planted.txt", box.tmp / "planted.txt",
                 box.home / "skills-out" / "escape" / "planted.txt", "/planted.txt", "/usr/planted.txt", box.home / "canary.txt"):
        out, result, _ = attempt(box, "write", layer, path=str(path))
        assert out["escaped"] is False, (path, out)
        assert not os.path.exists(path) or str(path).endswith("canary.txt"), path
    if layer != "landlock" or caps().landlock_abi >= 3:
        out, _, _ = attempt(box, "write", layer, path=str(box.docs / "a.txt"))  # overwrite (and so empty) a file it may only read
        assert out["escaped"] is False and (box.docs / "a.txt").read_text() == before
    assert (box.home / "canary.txt").read_text() == CANARY_HOME  # Muse's own file, untouched


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_cannot_follow_a_symbolic_link_out(box, proxy, layer):
    need(layer)
    (box.docs / "link-to-file").symlink_to(box.outside / "passwd")
    (box.docs / "link-to-folder").symlink_to(box.outside)
    (box.docs / "link-to-home").symlink_to(box.home)
    for path in (box.docs / "link-to-file", box.docs / "link-to-folder" / "passwd", box.docs / "link-to-home" / "canary.txt"):
        out, result, _ = attempt(box, "read", layer, path=str(path))
        assert out["escaped"] is False and not leaked(result, CANARY_OUTSIDE, CANARY_HOME), (path, out)
    out, _, _ = attempt(box, "write", layer, path=str(box.docs / "link-to-folder" / "planted.txt"))
    assert out["escaped"] is False and not (box.outside / "planted.txt").exists()


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_cannot_plant_a_link_in_its_data_folder_that_reaches_out(box, proxy, layer):
    need(layer)
    command = f"ln -s {box.outside}/passwd {box.data}/out && cat {box.data}/out"
    out, result, _ = attempt(box, "shell", layer, command=command)
    assert not leaked(result, CANARY_OUTSIDE), out


@pytest.mark.parametrize("layer", FS_LAYERS)
def test_cannot_read_another_process_through_proc(box, proxy, layer, victim):
    need(layer)
    for path in (f"/proc/{victim.pid}/environ", f"/proc/{os.getpid()}/environ", f"/proc/{victim.pid}/cmdline", "/proc/1/environ"):
        out, result, _ = attempt(box, "read", layer, path=path)
        assert out["escaped"] is False and not leaked(result, CANARY_ENV), (path, out)


# ================================================================ network


@pytest.mark.parametrize("layer", NET_LAYERS + ["landlock"])
def test_cannot_open_a_tcp_connection(box, proxy, layer):
    need(layer, landlock_abi=4)
    listener = Listener("tcp")
    out, result, report = attempt(box, "tcp", layer, host="127.0.0.1", port=listener.port)
    assert out["escaped"] is False and listener.close() == [], out
    assert report.net == ("tcp blocked" if layer == "landlock" else "blocked")


@pytest.mark.parametrize("layer", NET_LAYERS)
def test_cannot_send_udp(box, proxy, layer):
    need(layer)
    listener = Listener("udp")
    out, result, report = attempt(box, "udp", layer, host="127.0.0.1", port=listener.port)
    assert listener.close() == [] and report.net == "blocked", out  # nothing arrived outside
    if layer != "bwrap":  # inside bubblewrap's empty network the send "succeeds" and goes nowhere
        assert out["escaped"] is False, out


@pytest.mark.parametrize("layer", NET_LAYERS)
def test_cannot_connect_to_a_unix_socket(box, proxy, layer):
    need(layer)
    folders = (box.outside,) if layer == "bwrap" else (box.outside, box.docs)  # seccomp: even one inside a folder it may read
    for folder in folders:
        listener = Listener("unix", str(folder / "service.sock"))
        out, result, _ = attempt(box, "unix", layer, path=str(folder / "service.sock"))
        assert out["escaped"] is False and listener.close() == [], (folder, out)


def test_known_gap_bubblewrap_alone_reaches_a_unix_socket_inside_a_named_folder(box, proxy):
    """A socket file inside a folder the person named is shared with that folder. Only seccomp (no sockets at all) stops it."""
    need("bwrap")
    listener = Listener("unix", str(box.docs / "service.sock"))
    out, result, report = attempt(box, "unix", "bwrap", path=str(box.docs / "service.sock"))
    got = listener.close()
    assert any("unix socket inside a folder named for this run" in line for line in report.best_effort)  # the report says so
    assert out["escaped"] is True and got, "this gap seems closed here: update docs/sandbox.md and this test"


def test_known_gap_landlock_alone_is_not_a_network_block(box, proxy):
    """Landlock (at the ABIs used) blocks TCP only. The report must say so, and `require` must not accept it."""
    need("landlock", landlock_abi=4)
    out, result, report = attempt(box, "python", "landlock")
    assert report.net == "tcp blocked" and not report.isolated and "NOT fully isolated" in report.sentence()
    assert any("only TCP is blocked" in line for line in report.best_effort)
    from test_sandbox_support import LAYERS, run
    refused, refused_report = run(box, "escape", {"attempt": "python"}, "landlock", mode="require")
    assert refused.status == "failed" and refused_report.ran == "refused"


# ================================================================ processes


@pytest.mark.parametrize("layer", ["bwrap", "landlock", "seccomp", "all"])
def test_cannot_signal_a_process_outside(box, proxy, layer, victim):
    need(layer, landlock_abi=6)
    out, result, _ = attempt(box, "signal", layer, pid=victim.pid)
    assert out["escaped"] is False and out["error"] in ("PermissionError", "ProcessLookupError"), out
    time.sleep(0.2)
    assert victim.poll() is None and alive(victim.pid)  # the victim is still running
    out, _, _ = attempt(box, "signal", layer, pid=-1)  # "every process I may signal"
    assert victim.poll() is None and alive(os.getpid())


@pytest.mark.parametrize("layer", ["landlock", "seccomp", "all"])
def test_cannot_signal_its_parent(box, proxy, layer):
    need(layer, landlock_abi=6)
    if layer == "all" and caps().bwrap_works:
        pytest.skip("under bubblewrap the child's parent is bubblewrap's own helper inside the new process namespace, "
                    "not Muse; the real parent is covered by test_cannot_signal_a_process_outside[bwrap]")
    out, _, _ = attempt(box, "signal-parent", layer)
    assert out == {"escaped": False, "error": "PermissionError", "detail": 1}, out


@pytest.mark.parametrize("layer", ["landlock", "all"])
def test_cannot_start_a_shell(box, proxy, layer):
    need(layer)
    out, result, _ = attempt(box, "shell", layer, command="echo ran")
    assert out["escaped"] is False and out["error"] == "PermissionError", out  # Landlock grants no execute right anywhere


def test_a_shell_inside_bubblewrap_alone_is_still_inside(box, proxy, victim):
    """bubblewrap by itself does not stop `exec`; the shell it starts sees the same small world."""
    need("bwrap")
    command = (f"cat {box.home}/canary.txt {box.outside}/passwd /proc/{victim.pid}/environ; ls /home /root; "
               f"kill -9 {victim.pid}; echo x > {box.outside}/planted.txt; echo done")
    out, result, _ = attempt(box, "shell", "bwrap", command=command)
    assert out["escaped"] and out["detail"]["out"].strip().endswith("done")  # it ran...
    assert not leaked(result, CANARY_HOME, CANARY_OUTSIDE, CANARY_ENV)        # ...and reached nothing
    assert victim.poll() is None and not (box.outside / "planted.txt").exists()


@pytest.mark.parametrize("layer", ["seccomp", "all"])
def test_cannot_trace_or_read_the_memory_of_another_process(box, proxy, layer, victim):
    need(layer)
    out, result, _ = attempt(box, "read", layer, path=f"/proc/{victim.pid}/mem")
    assert out["escaped"] is False, out
    code = ("import ctypes,os;l=ctypes.CDLL(None,use_errno=True);"
            f"print(l.ptrace(16,{victim.pid},0,0), ctypes.get_errno())")  # PTRACE_ATTACH
    out, result, _ = attempt(box, "shell", layer, command=f"{sys.executable} -c '{code}'")
    assert out["escaped"] is False or "-1" in out["detail"]["out"], out
    assert victim.poll() is None


# ================================================================ disk


@pytest.mark.parametrize("layer", ["bwrap", "landlock", "all"])
def test_cannot_fill_the_disk_through_its_data_folder(box, proxy, layer):
    need(layer)
    import dataclasses

    from test_sandbox_support import LAYERS
    policy = dataclasses.replace(LAYERS[layer], data_bytes=4 << 20, file_bytes=1 << 20, watch_seconds=0.05)
    out, result, report = attempt(box, "big-file", layer, bytes=16 << 20, policy=policy)
    assert out["escaped"] is False and out["detail"] == 27 and (box.data / "big.bin").stat().st_size <= 1 << 20  # EFBIG
    (box.data / "big.bin").unlink()
    out, result, report = attempt(box, "fill", layer, files=100_000, size=512 << 10, policy=policy)
    assert result.status == "failed" and report.stopped == "data-cap"
    assert sum(f.stat().st_size for f in box.data.iterdir()) < 1 << 30


def test_the_temp_folder_is_size_capped_under_bubblewrap(box, proxy):
    need("bwrap")
    if not caps().bwrap_tmp_size:
        pytest.skip("this bubblewrap has no --size option, so the temp folder cannot be capped")
    import dataclasses

    from test_sandbox_support import LAYERS
    policy = dataclasses.replace(LAYERS["bwrap"], tmp_bytes=2 << 20)
    out, result, _ = attempt(box, "write", "bwrap", path="/sandbox-tmp/small.bin", policy=policy)
    assert out["escaped"] is True  # the temp folder is usable...
    command = "dd if=/dev/zero of=/sandbox-tmp/big.bin bs=1M count=8 2>&1; ls -l /sandbox-tmp/big.bin | cut -d' ' -f5"
    out, result, _ = attempt(box, "shell", "bwrap", command=command, policy=policy)
    assert "No space left" in out["detail"]["out"], out  # ...up to its cap
