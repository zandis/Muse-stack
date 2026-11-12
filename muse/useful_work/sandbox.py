"""Run a Lean process with no secrets, no network, and resource limits.

Production path (documented in ``docs/LEAN.md``): gVisor, Firecracker, or
bubblewrap. This module uses ``bwrap`` when it is on PATH, and always:

* passes a scrubbed environment (no provider keys, no ``LITELLM_MASTER_KEY``);
* sets CPU, address-space, file-descriptor, and file-size rlimits;
* runs in a fresh temporary directory;
* applies a wall-clock timeout;
* never inherits the caller's ``os.environ``.

If ``bwrap`` is missing, the process is still run with the empty env and
rlimits (fail-closed enough for a laptop; not a production jail). Real
mode must not be pointed at untrusted proofs on a machine that holds
production keys without one of the production jails.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


# Environment names we never forward, even if a caller tries to.
_SECRET_SUFFIXES = ("_API_KEY", "_MASTER_KEY", "_SALT_KEY", "_PASSWORD", "_SECRET", "_TOKEN")


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    used_bwrap: bool
    argv: list[str]


def _scrubbed_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "USER": "muse",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": str(home / "tmp"),
        # Lean looks at these; never copy them from the parent.
        "LEAN_PATH": "",
        "LAKE_CACHE_DIR": str(home / ".lake"),
    }
    if extra:
        for key, value in extra.items():
            upper = key.upper()
            if upper in {"DATABASE_URL", "AUTHORIZATION"} or any(upper.endswith(s) for s in _SECRET_SUFFIXES):
                continue
            if not value:
                continue
            # Allow only a short allow-list of Lean toolchain vars.
            if key in {"LEAN_PATH", "LEAN_SYSROOT", "LAKE_HOME", "ELAN_HOME", "PATH"}:
                env[key] = value
    return env


def _preexec(cpu_seconds: int, memory_bytes: int, nfiles: int, fsize: int):
    def inner() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        except (ValueError, resource.error, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, resource.error, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (nfiles, nfiles))
        except (ValueError, resource.error, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        except (ValueError, resource.error, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        except (ValueError, resource.error, OSError):
            pass
        try:
            # No core dumps.
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, resource.error, OSError):
            pass

    return inner


def _binds_for(binary: str) -> list[str]:
    """Read-only bind mount points needed to execute ``binary``."""
    binds = ["/usr", "/bin", "/lib", "/lib64", "/etc/alternatives"]
    resolved = Path(binary).resolve()
    binds.append(str(resolved.parent))
    # elan / nix / lean toolchains often live under HOME or /nix or ~/.elan
    for candidate in (
        resolved.parents[2] if len(resolved.parents) >= 3 else None,
        Path.home() / ".elan",
        Path("/nix"),
        Path("/opt/lean"),
        Path("/usr/local"),
    ):
        if candidate and candidate.exists():
            binds.append(str(candidate))
    # Dedup while keeping order.
    seen: set[str] = set()
    out: list[str] = []
    for item in binds:
        if item not in seen and Path(item).exists():
            seen.add(item)
            out.append(item)
    return out


def run_sandboxed(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    extra_env: dict[str, str] | None = None,
    memory_bytes: int = 1 << 30,
    cpu_seconds: int = 30,
    input_text: str | None = None,
) -> SandboxResult:
    """Run ``argv`` in ``cwd`` with a secret-free env and resource limits.

    Uses ``bwrap --unshare-net`` when bubblewrap is available.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "tmp").mkdir(exist_ok=True)
    env = _scrubbed_env(cwd, extra_env)

    def _invoke(cmd: list[str], *, used: bool) -> SandboxResult:
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                cwd=str(cwd),
                env=env if not used else {},
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                input=input_text,
                preexec_fn=_preexec(cpu_seconds, memory_bytes, nfiles=64, fsize=20 << 20),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return SandboxResult(
                returncode=-1,
                stdout=stdout,
                stderr=stderr or f"timed out after {timeout_seconds}s",
                timed_out=True,
                used_bwrap=used,
                argv=cmd,
            )
        except OSError as exc:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"sandbox failed to exec: {exc}",
                timed_out=False,
                used_bwrap=used,
                argv=cmd,
            )
        return SandboxResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
            used_bwrap=used,
            argv=cmd,
        )

    bwrap = shutil.which("bwrap")
    if bwrap:
        binds: list[str] = []
        for path in _binds_for(argv[0]):
            binds.extend(["--ro-bind-try", path, path])
        bwrap_cmd = [
            bwrap,
            "--unshare-net",
            "--unshare-pid",
            "--die-with-parent",
            "--tmpfs", "/tmp",
            "--dev", "/dev",
            "--proc", "/proc",
            "--bind", str(cwd), str(cwd),
            "--chdir", str(cwd),
            *binds,
            "--clearenv",
        ]
        for key, value in env.items():
            bwrap_cmd.extend(["--setenv", key, value])
        bwrap_cmd.extend(["--", *argv])
        result = _invoke(bwrap_cmd, used=True)
        namespace_failed = (
            result.returncode != 0
            and "namespace" in (result.stderr or "").lower()
        )
        if not namespace_failed:
            return result
        # User namespaces are often blocked in CI containers. Keep the
        # scrubbed env, rlimits, temp dir, and timeout, and fall back
        # without bwrap rather than failing closed on the jail itself.
        # Production should still use gVisor / Firecracker / a host that
        # allows bubblewrap.
    return _invoke(list(argv), used=False)




def make_work_dir(prefix: str = "muse-lean-") -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=prefix)
