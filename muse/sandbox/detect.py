"""Find out what this machine can really enforce, by trying each layer in a throw-away process.

`probe()` is what `muse doctor` prints and what a run consults before it
starts. Nothing is assumed from version numbers: bubblewrap counts only if a
Python child came up inside it with the same options a run uses, Landlock
only if a read was really denied, seccomp only if a socket was really refused.
The result is kept for the life of the process (`probe(refresh=True)` asks again).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile

from muse.sandbox import launch
from muse.sandbox.policy import Capabilities, Policy

TIMEOUT_S = 20.0
_cached: Capabilities | None = None


def _run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=TIMEOUT_S, close_fds=True)
    except subprocess.TimeoutExpired:
        return -1, "it did not answer in time"
    except OSError as exc:
        return -1, type(exc).__name__
    words = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return done.returncode, " ".join(words[-1].split())[:160] if words else ""


def _selftest_argv(kind: str) -> list[str]:
    return [sys.executable, "-I", "-B", "-c", launch.BOOT, launch.repo_root(), "--selftest", kind]


def _clean_env() -> dict[str, str]:
    return launch.child_env(Policy(), {}, tempfile.gettempdir())


def _probe_bwrap(caps: dict) -> None:
    path = shutil.which("bwrap") or ""
    caps["bwrap_path"] = path
    if not path:
        caps["bwrap_reason"] = "bubblewrap is not installed"
        return
    try:
        done = subprocess.run([path, "--version"], capture_output=True, timeout=TIMEOUT_S)
        caps["bwrap_version"] = done.stdout.decode("utf-8", "replace").strip()[:40]
    except (OSError, subprocess.TimeoutExpired):
        pass
    env = launch.child_env(Policy(), {}, launch.INNER_TMP)
    runtime = launch.runtime_paths()
    muse_pkg = os.path.join(launch.repo_root(), "muse")
    for size in (Policy().tmp_bytes, None):
        argv = launch.bwrap_argv(path, env, runtime, muse_pkg, [], None, [], size) + ["--"] + _selftest_argv("start")
        code, words = _run(argv, env)
        if code == 0:
            caps["bwrap_works"], caps["bwrap_tmp_size"], caps["bwrap_reason"] = True, size is not None, ""
            return
        caps["bwrap_reason"] = words or f"it ended with code {code}"


def _probe_linux(caps: dict) -> None:
    from muse.sandbox import lockdown

    _probe_bwrap(caps)
    caps["landlock_abi"] = lockdown.landlock_abi()
    if caps["landlock_abi"] < 1:
        caps["landlock_reason"] = "the kernel does not offer it, or it is switched off"
    else:
        code, words = _run(_selftest_argv("landlock"), _clean_env())
        caps["landlock_works"] = code == 0
        caps["landlock_reason"] = "" if code == 0 else f"the self-test failed (code {code})"
    if platform.machine() not in lockdown.AUDIT_ARCH:
        caps["seccomp_reason"] = f"no filter table for {platform.machine()}"
    else:
        code, words = _run(_selftest_argv("seccomp"), _clean_env())
        caps["seccomp_works"] = code == 0
        caps["seccomp_reason"] = "" if code == 0 else f"the self-test failed (code {code})"
    caps["no_new_privs"] = True  # prctl(PR_SET_NO_NEW_PRIVS) exists on every kernel that has either layer; each run checks it
    caps["cgroup_v2"] = os.path.exists("/sys/fs/cgroup/cgroup.controllers")


def _probe_macos(caps: dict) -> None:
    """UNTESTED: written without a Mac. The profile is used only if this self-test passes on the machine."""
    path = "/usr/bin/sandbox-exec"
    if not os.path.exists(path):
        return
    tmp = tempfile.mkdtemp(prefix="muse-sandbox-probe-")
    try:
        profile = launch.macos_profile(launch.runtime_paths(), os.path.join(launch.repo_root(), "muse"), [], None, tmp)
        code, _ = _run([path, "-p", profile] + _selftest_argv("start"), _clean_env())
        if code == 0:
            caps["sandbox_exec_path"] = path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe(refresh: bool = False) -> Capabilities:
    """What isolation this machine offers. Never raises."""
    global _cached
    if _cached is not None and not refresh:
        return _cached
    caps: dict = {"platform": "linux" if sys.platform.startswith("linux") else sys.platform, "machine": platform.machine(),
                  "running_as_root": hasattr(os, "geteuid") and os.geteuid() == 0}
    try:
        if caps["platform"] == "linux":
            _probe_linux(caps)
        elif caps["platform"] == "darwin":
            _probe_macos(caps)
    except Exception as exc:  # a probe must never break `muse doctor`
        caps.setdefault("bwrap_reason", f"the probe stopped ({type(exc).__name__})")
    _cached = Capabilities(**caps)
    return _cached
