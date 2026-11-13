"""Build the command, the environment and the path lists for one sandboxed child. Starts nothing.

The child is always `python -I -B` (isolated mode: no PYTHON* variables, no
user site, no current folder on the import path) running
`muse.sandbox.child`. What differs per platform is what wraps it:

    Linux    bubblewrap when it works here (new user, mount, pid, ipc, uts,
             cgroup and network namespaces; a root made only of the paths
             below), and in every case the path lists for Landlock, which the
             child applies to itself
    macOS    `sandbox-exec` with a generated profile. UNTESTED: written
             without a Mac. It is used only when its self-test passes on the
             machine, and never counts as isolation.
    other    nothing: a plain child process with an emptied environment

The paths a child may see:
    read only   the Python runtime, the `muse` package, the folders named for this run
    read+write  the skill's `data` folder (when it declared one) and a private temp folder
"""

from __future__ import annotations

import os
import sys
import sysconfig
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from muse.sandbox.policy import Capabilities, Policy

BOOT = "import sys; sys.path.append(sys.argv[1]); from muse.sandbox.child import main; main(sys.argv[2:])"
SYSTEM_READ = ("/usr", "/lib", "/lib64", "/lib32", "/libx32", "/bin")
SYSTEM_FILES = ("/etc/ld.so.cache", "/etc/localtime")
DEVICES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")
CHILD_PATH = "/usr/bin:/bin"
INNER_TMP = "/sandbox-tmp"  # the private temp folder's name inside bubblewrap's root


class Unavailable(Exception):
    """The sandbox cannot be set up for this run. The message is one plain sentence."""


@dataclass
class Plan:
    argv: list[str]
    env: dict[str, str]
    pass_fds: tuple[int, ...]
    bwrap: bool = False
    macos_profile: bool = False
    landlock: dict[str, list[str]] | None = None  # {"read": [...], "write": [...], "devices": [...]} for the child, or None
    seccomp: bool = False
    tmp: str = ""                # the temp folder as the child sees it
    host_tmp: str = ""           # the same folder on the host when the parent made it (and must delete it)
    notes: list[str] = field(default_factory=list)  # layers that are not used, and why


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(path))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def runtime_paths() -> list[str]:
    """Folders the Python that will run the child needs to read: its prefixes, standard library and site-packages."""
    found: list[str] = []
    candidates = [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(_real(sys.executable))]
    candidates += [value for key, value in sysconfig.get_paths().items() if key in ("stdlib", "platstdlib", "purelib", "platlib")]
    for candidate in candidates:
        for path in dict.fromkeys((os.path.abspath(candidate), _real(candidate))):
            if os.path.isdir(path) and not any(_inside(path, known) for known in found + list(SYSTEM_READ)):
                found = [known for known in found if not _inside(known, path)] + [path]
    return found


def check_paths(read_roots: Sequence[str], runtime: Sequence[str], muse_home: str, repo_root: str) -> None:
    """Refuse path sets that would hand the child Muse's own home or the whole disk."""
    home = _real(muse_home)
    user_home = _real(os.path.expanduser("~"))
    for root in read_roots:
        if root == "/":
            raise Unavailable("the whole disk cannot be given to a skill; name a folder")
        if _inside(home, root):
            raise Unavailable("a folder named for this run contains Muse's own folder (its keys and records); name a narrower folder")
    for path in runtime:
        if path == "/" or path == user_home or _inside(home, path):
            raise Unavailable("the Python that runs Muse lives in a folder that cannot be shared with a skill safely")
    if _inside(home, _real(os.path.join(repo_root, "muse"))):
        raise Unavailable("Muse's own folder is inside its program folder; move MUSE_HOME")


def child_env(policy: Policy, parent: Mapping[str, str], tmp: str) -> dict[str, str]:
    """The child's whole environment: fixed PATH, a temp HOME, and the few allowed names. Nothing else."""
    env = {"PATH": CHILD_PATH, "HOME": tmp + "/home", "TMPDIR": tmp, "LANG": "C.UTF-8"}
    for name in policy.env_allow:
        value = str(parent.get(name, "") or "")
        if value and "\x00" not in value and len(value) < 256:
            env[name] = value
    return env


def python_argv(repo_root: str, fds: tuple[int, int], extra: Sequence[str] = ()) -> list[str]:
    return [sys.executable, "-I", "-B", "-X", "utf8", "-c", BOOT, repo_root, str(fds[0]), str(fds[1]), *extra]


def bwrap_argv(bwrap: str, env: Mapping[str, str], runtime: Sequence[str], muse_pkg: str, read_roots: Sequence[str],
               data_dir: str | None, extra_read: Sequence[str], tmp_bytes: int | None) -> list[str]:
    """bubblewrap options for a root that holds only what the child may see. The command to run is appended by the caller."""
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--hostname", "sandbox", "--clearenv"]
    for name, value in env.items():
        args += ["--setenv", name, value]
    for path in SYSTEM_READ:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            args += ["--ro-bind", path, path]
    for path in SYSTEM_FILES:
        args += ["--ro-bind-try", path, path]
    args += ["--dev", "/dev"]  # bubblewrap's own small /dev: null, zero, full, random, urandom, tty. No /proc, no /sys.
    for path in list(runtime) + [muse_pkg] + list(extra_read) + list(read_roots):
        args += ["--ro-bind", path, path]
    if data_dir:
        args += ["--bind", data_dir, data_dir]
    if tmp_bytes:
        args += ["--size", str(int(tmp_bytes))]
    args += ["--tmpfs", INNER_TMP, "--dir", INNER_TMP + "/home", "--chdir", INNER_TMP]
    return args


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(runtime: Sequence[str], muse_pkg: str, read_roots: Sequence[str], data_dir: str | None, tmp: str) -> str:
    """A `sandbox-exec` profile: deny by default, read the runtime and the named folders, write data and temp, no network.

    UNTESTED. Written from the public description of the profile language without a Mac to run it on.
    `sandbox-exec` is marked deprecated by Apple although the system still uses the same mechanism.
    """
    read = ["/usr/lib", "/usr/share", "/System/Library", "/Library/Frameworks", "/private/var/db/timezone", "/dev/null",
            "/dev/urandom", "/dev/random", "/dev/zero", *runtime, muse_pkg, *read_roots]
    write = [tmp] + ([data_dir] if data_dir else [])
    lines = ["(version 1)", "(deny default)", "(deny network*)", "(allow process-fork)", "(allow signal (target self))",
             "(allow sysctl-read)", "(allow file-read-metadata)", "(allow file-write-data (literal \"/dev/null\"))"]
    lines.append("(allow file-read* " + " ".join(f"(subpath {_quote(p)})" for p in dict.fromkeys(read + write)) + ")")
    lines.append("(allow file-write* " + " ".join(f"(subpath {_quote(p)})" for p in write) + ")")
    lines.append(f"(allow process-exec (literal {_quote(_real(sys.executable))}) (literal {_quote(sys.executable)}))")
    return "\n".join(lines) + "\n"


def make(caps: Capabilities, policy: Policy, *, read_roots: Sequence[str], data_dir: str | None, muse_home: str,
         repo_root: str, parent_env: Mapping[str, str], fds: tuple[int, int], child_args: Sequence[str] = ()) -> Plan:
    """Decide the layers for this run and build the command. Raises Unavailable for path sets that cannot be made safe."""
    roots = [_real(r) for r in read_roots]
    extra = [_real(p) for p in policy.extra_read]
    data = _real(data_dir) if data_dir else None
    runtime = runtime_paths()
    muse_pkg = _real(os.path.join(repo_root, "muse"))
    check_paths(roots, runtime, muse_home, repo_root)
    notes: list[str] = []

    use_bwrap = caps.bwrap_works and policy.wants("bwrap")
    use_landlock = caps.landlock_works and policy.wants("landlock")
    use_seccomp = caps.seccomp_works and policy.wants("seccomp")
    if caps.platform == "linux":
        if not use_bwrap:
            notes.append("bubblewrap is not used" + (f" ({caps.bwrap_reason})" if not caps.bwrap_works and caps.bwrap_reason else ""))
        if not use_landlock:
            notes.append("Landlock is not used" + (f" ({caps.landlock_reason})" if not caps.landlock_works and caps.landlock_reason else ""))
        if not use_seccomp:
            notes.append("the seccomp filter is not used" + (f" ({caps.seccomp_reason})" if not caps.seccomp_works and caps.seccomp_reason else ""))

    host_tmp = ""
    if use_bwrap:
        tmp = INNER_TMP
    else:
        host_tmp = tempfile.mkdtemp(prefix="muse-sandbox-")
        os.mkdir(os.path.join(host_tmp, "home"), 0o700)
        tmp = _real(host_tmp)
    env = child_env(policy, parent_env, tmp)
    command = python_argv(repo_root, fds, child_args)
    plan = Plan(argv=command, env=env, pass_fds=fds, seccomp=use_seccomp, tmp=tmp, host_tmp=host_tmp, notes=notes)

    if use_landlock:
        plan.landlock = {
            "read": [p for p in SYSTEM_READ if os.path.exists(p)] + [p for p in SYSTEM_FILES if os.path.exists(p)]
                    + runtime + [muse_pkg] + extra + roots,
            "write": [tmp] + ([data] if data else []),
            "devices": [p for p in DEVICES if os.path.exists(p)],
        }
    if use_bwrap:
        size = policy.tmp_bytes if caps.bwrap_tmp_size else None
        if not size:
            notes.append("this bubblewrap cannot cap the temp folder's size")
        plan.argv = bwrap_argv(caps.bwrap_path, env, runtime, muse_pkg, roots, data, extra, size) + ["--"] + command
        plan.bwrap = True
    elif caps.platform == "darwin" and caps.sandbox_exec_path and policy.wants("bwrap"):
        plan.argv = [caps.sandbox_exec_path, "-p", macos_profile(runtime, muse_pkg, roots, data, tmp)] + command
        plan.macos_profile = True
        notes.append("the macOS sandbox profile is used but was never tested by its author, so it does not count as isolation")
    return plan


def repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])
