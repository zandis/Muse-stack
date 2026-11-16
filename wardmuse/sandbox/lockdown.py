"""What the child does to itself before any skill code runs (Linux): limits, no new privileges, Landlock, seccomp.

Standard library only, no C extension: the kernel interfaces are reached with
ctypes. Every step returns what it REALLY did; a step that could not be done
says so in words and the caller decides (docs/sandbox.md). Nothing here is
undone later: all of it is one-way for this process and its children.

Landlock (kernel.org, "Landlock: unprivileged access control"): a ruleset
lists the access rights it handles; everything handled and not allowed by a
rule is denied. The ABI version says which rights exist:

    1  the base file rights      2  REFER (rename/link across folders)
    3  TRUNCATE                  4  TCP bind and connect
    5  IOCTL on devices          6  scopes: signals, abstract unix sockets
    7  logging flags (unused here). Later ABIs (9: pathname unix sockets,
       10: UDP) are NOT used yet: their constants were not verified here.

Not restricted by Landlock at any ABI used here: stat/access/chdir/chmod on
paths, UDP, pathname unix sockets. The seccomp filter below closes the
socket part: with it no new socket of any kind can be created.

seccomp: one small classic-BPF filter, built here per architecture
(x86_64 and aarch64; the aarch64 numbers are from the kernel's generic table
and were NOT run on aarch64 by the author). It is a deny-list on top of the
other layers, not a full allow-list.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------- libc


def _libc() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def _syscall(number: int, *args: Any) -> int:
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(result)


def _prctl(option: int, a2: int = 0, a3: int = 0, a4: int = 0, a5: int = 0) -> int:
    libc = _libc()
    libc.prctl.restype = ctypes.c_int
    result = libc.prctl(ctypes.c_int(option), ctypes.c_ulong(a2), ctypes.c_ulong(a3), ctypes.c_ulong(a4), ctypes.c_ulong(a5))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(result)


PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_SET_SECCOMP = 22
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
SIGKILL = 9

# ---------------------------------------------------------------- small steps


def die_with_parent() -> bool:
    try:
        _prctl(PR_SET_PDEATHSIG, SIGKILL)
        return True
    except OSError:
        return False


def no_new_privs() -> bool:
    """After this, no exec can gain privileges (setuid bits, file capabilities). Needed by Landlock and seccomp."""
    try:
        _prctl(PR_SET_NO_NEW_PRIVS, 1)
        return _prctl(PR_GET_NO_NEW_PRIVS) == 1
    except OSError:
        return False


def not_dumpable() -> bool:
    try:
        _prctl(PR_SET_DUMPABLE, 0)
        return True
    except OSError:
        return False


def drop_capabilities() -> bool:
    """Empty every capability set (matters when the parent is root; a no-op for an ordinary user)."""
    for cap in range(64):
        try:
            _prctl(PR_CAPBSET_DROP, cap)
        except OSError:
            pass  # EPERM without CAP_SETPCAP (an ordinary user: nothing to drop), EINVAL past the last capability
    try:
        _prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL)
    except OSError:
        pass
    header = struct.pack("=Ii", 0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3, this process
    data = ctypes.create_string_buffer(struct.pack("=6I", 0, 0, 0, 0, 0, 0))
    libc = _libc()
    libc.capset.restype = ctypes.c_int
    return libc.capset(ctypes.c_char_p(header), data) == 0


def set_rlimits(limits: dict[str, int]) -> dict[str, int]:
    """Apply {AS, CPU, NOFILE, NPROC, FSIZE, CORE}. Returns the ones that were really set."""
    import resource

    done: dict[str, int] = {}
    for name, value in limits.items():
        which = getattr(resource, f"RLIMIT_{name}", None)
        if which is None:
            continue
        try:
            # CPU: the soft limit sends SIGXCPU (so the parent can say "processor time"), the hard one, 2 s later, SIGKILL.
            resource.setrlimit(which, (int(value), int(value) + (2 if name == "CPU" else 0)))
            done[name] = int(value)
        except (ValueError, OSError):
            pass
    return done


def close_other_fds(keep: Iterable[int]) -> int:
    """Close every descriptor except stdio and `keep`. Returns how many were open and are now closed."""
    wanted = {0, 1, 2, *keep}
    closed = 0
    try:
        top = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        top = 4096
    top = min(max(top, 256), 65536)
    for fd in range(3, top):
        if fd in wanted:
            continue
        try:
            os.close(fd)
            closed += 1
        except OSError:
            pass
    return closed


# ---------------------------------------------------------------- Landlock

NR_LANDLOCK_CREATE_RULESET = 444  # the same on every architecture
NR_LANDLOCK_ADD_RULE = 445
NR_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REMOVE_DIR, FS_REMOVE_FILE, FS_MAKE_CHAR, FS_MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
FS_MAKE_REG, FS_MAKE_SOCK, FS_MAKE_FIFO, FS_MAKE_BLOCK, FS_MAKE_SYM = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
FS_REFER, FS_TRUNCATE, FS_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1

FS_BASE = (1 << 13) - 1  # ABI 1: bits 0..12
FS_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV  # the only rights a FILE rule may carry
FS_READ = FS_READ_FILE | FS_READ_DIR
# A folder the skill owns: read, write, create and delete plain files and folders. Never execute, never
# devices, sockets, fifos or symbolic links.
FS_WRITE = FS_READ | FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_DIR | FS_MAKE_REG | FS_REFER | FS_TRUNCATE


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is absent or switched off. No side effect."""
    try:
        return max(0, _syscall(NR_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION)))
    except OSError:
        return 0


def handled_fs(abi: int) -> int:
    mask = FS_BASE
    if abi >= 2:
        mask |= FS_REFER
    if abi >= 3:
        mask |= FS_TRUNCATE
    if abi >= 5:
        mask |= FS_IOCTL_DEV
    return mask


def apply_landlock(read: Sequence[str], write: Sequence[str], devices: Sequence[str] = ()) -> dict[str, Any]:
    """Deny by default; allow reading under `read`, reading and writing under `write`, read/write of `devices`.

    Returns {"abi": N, "full": bool, "net_tcp": bool, "scoped": bool, "missing": [words], "skipped": n}.
    Raises OSError when Landlock cannot be applied at all. `no_new_privs()` must have succeeded first.
    """
    abi = landlock_abi()
    if abi < 1:
        raise OSError(errno.ENOSYS, "Landlock is not available")
    fs = handled_fs(abi)
    net = (NET_BIND_TCP | NET_CONNECT_TCP) if abi >= 4 else 0
    scoped = (SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if abi >= 6 else 0
    attr = _RulesetAttr(fs, net, scoped)
    size = 8 if abi < 4 else (16 if abi < 6 else 24)  # older kernels know a shorter struct
    ruleset = _syscall(NR_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    skipped = 0
    try:
        for paths, rights in ((read, FS_READ), (write, FS_WRITE), (devices, FS_READ_FILE | FS_WRITE_FILE)):
            for path in dict.fromkeys(paths):
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    skipped += 1  # the path does not exist: nothing to allow
                    continue
                try:
                    allowed = rights & fs
                    if not os.path.isdir(path):
                        allowed &= FS_FILE_RIGHTS
                    rule = _PathBeneathAttr(allowed, fd)
                    _syscall(NR_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                             ctypes.byref(rule), ctypes.c_uint32(0))
                finally:
                    os.close(fd)
        _syscall(NR_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    finally:
        os.close(ruleset)
    missing = []
    if abi < 3:
        missing.append(f"Landlock ABI {abi} cannot stop a readable file from being emptied (needs ABI 3)")
    if abi < 4:
        missing.append(f"Landlock ABI {abi} cannot block TCP (needs ABI 4)")
    if abi < 6:
        missing.append(f"Landlock ABI {abi} cannot stop signals to other processes or abstract unix sockets (needs ABI 6)")
    return {"abi": abi, "full": abi >= 3, "net_tcp": abi >= 4, "scoped": abi >= 6, "missing": missing, "skipped": skipped}


# ---------------------------------------------------------------- seccomp

AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_KILL_PROCESS = 0x80000000
RET_EPERM = 0x00050000 | errno.EPERM
X32_BIT = 0x40000000

# Refused outright (EPERM). Names -> (x86_64, aarch64). From the kernel's syscall tables.
DENY: dict[str, tuple[int, int]] = {
    # no new sockets of any kind (inherited pipes still work); io_uring can open sockets behind a filter's back
    "socket": (41, 198), "io_uring_setup": (425, 425), "io_uring_enter": (426, 426), "io_uring_register": (427, 427),
    # other processes' memory and descriptors
    "ptrace": (101, 117), "process_vm_readv": (310, 270), "process_vm_writev": (311, 271), "kcmp": (312, 272),
    "pidfd_open": (434, 434), "pidfd_getfd": (438, 438), "pidfd_send_signal": (424, 424), "process_madvise": (440, 440),
    # leaving the process group the parent kills
    "setsid": (112, 157), "setpgid": (109, 154),
    # namespaces and mounts
    "unshare": (272, 97), "setns": (308, 268), "mount": (165, 40), "umount2": (166, 39), "pivot_root": (155, 41),
    "chroot": (161, 51), "open_tree": (428, 428), "move_mount": (429, 429), "fsopen": (430, 430), "fsconfig": (431, 431),
    "fsmount": (432, 432), "mount_setattr": (442, 442), "open_by_handle_at": (304, 265),
    # kernel surfaces a skill never needs
    "bpf": (321, 280), "perf_event_open": (298, 241), "userfaultfd": (323, 282), "keyctl": (250, 219), "add_key": (248, 217),
    "request_key": (249, 218), "kexec_load": (246, 104), "kexec_file_load": (320, 294), "init_module": (175, 105),
    "finit_module": (313, 273), "delete_module": (176, 106), "reboot": (169, 142), "swapon": (167, 224), "swapoff": (168, 225),
    "acct": (163, 89), "quotactl": (179, 60), "personality": (135, 92),
}
# Allowed only when the first argument is this process's own id (signals to itself); anything else is refused.
OWN_PID_ONLY: dict[str, tuple[int, int]] = {
    "kill": (62, 129), "tkill": (200, 130), "tgkill": (234, 131), "rt_sigqueueinfo": (129, 138), "rt_tgsigqueueinfo": (297, 240),
}


def _stmt(code: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, 0, 0, k & 0xFFFFFFFF)


def _jeq(k: int, jt: int, jf: int) -> bytes:
    return struct.pack("=HBBI", 0x15, jt, jf, k & 0xFFFFFFFF)  # BPF_JMP | BPF_JEQ | BPF_K


def build_filter(machine: str, own_pid: int) -> bytes:
    """The filter as packed `struct sock_filter` records. Raises ValueError for an architecture it does not know."""
    if machine not in AUDIT_ARCH:
        raise ValueError(f"no seccomp table for {machine}")
    col = 0 if machine == "x86_64" else 1
    ld_abs, ret = 0x20, 0x06  # BPF_LD | BPF_W | BPF_ABS ; BPF_RET | BPF_K
    prog = [_stmt(ld_abs, 4), _jeq(AUDIT_ARCH[machine], 1, 0), _stmt(ret, RET_KILL_PROCESS), _stmt(ld_abs, 0)]
    if machine == "x86_64":  # the x32 ABI reuses this architecture value with bit 30 set in the number
        prog += [struct.pack("=HBBI", 0x35, 0, 1, X32_BIT), _stmt(ret, RET_KILL_PROCESS)]  # BPF_JMP | BPF_JGE | BPF_K
    for numbers in DENY.values():
        prog += [_jeq(numbers[col], 0, 1), _stmt(ret, RET_EPERM)]
    for numbers in OWN_PID_ONLY.values():
        # nr matches: load the low half of argument 0 (little-endian on both architectures) and compare with our pid
        prog += [_jeq(numbers[col], 0, 4), _stmt(ld_abs, 16), _jeq(own_pid, 0, 1), _stmt(ret, RET_ALLOW), _stmt(ret, RET_EPERM)]
    prog.append(_stmt(ret, RET_ALLOW))
    return b"".join(prog)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def apply_seccomp() -> bool:
    """Install the filter and CHECK it: afterwards creating a socket must fail. False when it could not be installed."""
    machine = platform.machine()
    try:
        program = build_filter(machine, os.getpid())
    except ValueError:
        return False
    buffer = ctypes.create_string_buffer(program, len(program))
    prog = _SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    libc = _libc()
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(ctypes.c_int(PR_SET_SECCOMP), ctypes.c_ulong(SECCOMP_MODE_FILTER), ctypes.byref(prog), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        return False
    return sockets_blocked()


def sockets_blocked() -> bool:
    import socket

    for family in (socket.AF_INET, socket.AF_UNIX):
        try:
            socket.socket(family, socket.SOCK_DGRAM).close()
            return False
        except PermissionError:
            continue
        except OSError:
            return False
    return True
