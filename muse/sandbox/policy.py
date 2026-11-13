"""What a sandboxed run is allowed, what this machine can enforce, and what actually was enforced.

Three small records, no logic that touches the system:

    Policy        the caps and switches for one run (resource limits, which layers to use)
    Capabilities  what `probe()` found on this machine
    Report        which layers were ACTIVE for one run, in words an app can show

and `mode_for(skill, requested, env)`, the rule that decides whether a skill
runs in the sandbox at all (docs/sandbox.md, "Modes").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

MODES = ("off", "auto", "require")
ENV_MODE = "MUSE_SANDBOX"
# Test-only way out of the rule "a skill that is not shipped with Muse always runs with `require`".
# Read from the PROCESS environment only. The runner's older tests use throw-away home skills to test the
# runner itself; tests/conftest.py sets it. Never set it on a real home.
ENV_UNSAFE_HOME_SKILLS = "MUSE_SANDBOX_UNSAFE_HOME_SKILLS"
UNSAFE_HOME_SKILLS_VALUE = "in-process-for-tests"

MIB = 1024 * 1024


@dataclass(frozen=True)
class Policy:
    """Limits and switches for one sandboxed run. The defaults are what `muse skills run` uses."""

    # Layers: None = use it when this machine has it; False = do not use it (tests, debugging); True = same as None.
    use_bwrap: bool | None = None
    use_landlock: bool | None = None
    use_seccomp: bool | None = None
    # Resource limits (setrlimit in the child, before any skill code runs).
    memory_bytes: int = 2048 * MIB      # RLIMIT_AS: address space
    open_files: int = 256               # RLIMIT_NOFILE
    processes: int = 64                 # RLIMIT_NPROC (threads count; see docs/sandbox.md for when the kernel enforces it)
    file_bytes: int = 1024 * MIB        # RLIMIT_FSIZE: the largest single file
    cpu_seconds: float | None = None    # RLIMIT_CPU; None = the job's `seconds` budget plus the grace
    # Limits the parent enforces.
    grace_seconds: float = 15.0         # wall clock allowed beyond the job's `seconds` budget (covers start-up)
    data_bytes: int = 4096 * MIB        # the skill's `data` folder, checked about once a second; over it, the run is ended
    tmp_bytes: int = 256 * MIB          # the private temp folder (a size-capped tmpfs under bubblewrap, else checked like data)
    frame_bytes: int = 64 * MIB         # the largest single message the child may send
    events: int = 20_000                # progress events accepted from the child; more are dropped
    # Environment variable NAMES copied from the parent when set. Everything else is absent in the child.
    env_allow: tuple[str, ...] = ("LANG", "LC_ALL", "TZ", "MUSE_TZ")
    # Extra read-only paths (tests). Never needed for a normal run.
    extra_read: tuple[str, ...] = ()
    # Called in the parent before each thing done on the skill's behalf: (action, facts, token). Raise
    # PermissionError to refuse. The permission compiler plugs in here; nothing is checked by default.
    authorize: Callable[[str, Mapping[str, Any], str | None], None] | None = field(default=None, compare=False)

    def wants(self, layer: str) -> bool:
        return getattr(self, f"use_{layer}") is not False


@dataclass(frozen=True)
class Capabilities:
    """What this machine can do. Every `*_works` flag comes from really trying it in a throw-away process."""

    platform: str = ""                  # linux | darwin | win32 | ...
    machine: str = ""                   # x86_64 | aarch64 | ...
    bwrap_path: str = ""
    bwrap_version: str = ""
    bwrap_works: bool = False           # a Python child really started inside new user, mount, pid and network namespaces
    bwrap_reason: str = ""              # why not, in a few words
    bwrap_tmp_size: bool = False        # `--size` is accepted, so the temp folder can be capped
    landlock_abi: int = 0               # 0 = not available
    landlock_works: bool = False        # a ruleset was applied in a child and a read outside it was denied
    landlock_reason: str = ""
    seccomp_works: bool = False         # the filter was installed in a child and socket() then failed
    seccomp_reason: str = ""
    no_new_privs: bool = False
    cgroup_v2: bool = False             # reported only; not used yet
    sandbox_exec_path: str = ""         # macOS; UNTESTED
    running_as_root: bool = False

    @property
    def fs_isolation(self) -> bool:
        """A layer that keeps a skill out of every file it was not given."""
        return self.bwrap_works or (self.landlock_works and self.landlock_abi >= 3)

    @property
    def net_isolation(self) -> bool:
        """A layer that leaves a skill with no network at all (TCP-only blocking does not count)."""
        return self.bwrap_works or self.seccomp_works

    @property
    def can_require(self) -> bool:
        return self.fs_isolation and self.net_isolation

    def missing(self) -> list[str]:
        out = []
        if not self.fs_isolation:
            out.append("nothing here can keep a skill out of your other files (needs bubblewrap, or Landlock with a kernel 6.2 or newer)")
        if not self.net_isolation:
            out.append("nothing here can cut a skill off from the network (needs bubblewrap, or seccomp)")
        return out

    def public(self) -> dict[str, Any]:
        return {
            "platform": self.platform, "machine": self.machine,
            "bwrap": {"path": self.bwrap_path, "version": self.bwrap_version, "works": self.bwrap_works,
                      "reason": self.bwrap_reason, "tmp_size": self.bwrap_tmp_size},
            "landlock": {"abi": self.landlock_abi, "works": self.landlock_works, "reason": self.landlock_reason},
            "seccomp": {"works": self.seccomp_works, "reason": self.seccomp_reason},
            "no_new_privs": self.no_new_privs, "cgroup_v2": self.cgroup_v2, "sandbox_exec": self.sandbox_exec_path,
            "running_as_root": self.running_as_root, "fs_isolation": self.fs_isolation,
            "net_isolation": self.net_isolation, "can_require": self.can_require,
        }

    def lines(self) -> list[str]:
        """Plain lines for `muse doctor`."""
        def yn(ok: bool, yes: str, no: str) -> str:
            return yes if ok else no

        out = [f"platform: {self.platform} {self.machine}".rstrip()]
        if self.platform == "linux":
            out.append("bubblewrap: " + yn(self.bwrap_works, f"works ({self.bwrap_version or self.bwrap_path})",
                                           f"not usable ({self.bwrap_reason or 'not installed'})"))
            out.append("Landlock: " + yn(self.landlock_works, f"works (ABI {self.landlock_abi})",
                                         f"not usable ({self.landlock_reason or 'the kernel does not offer it'})"))
            out.append("seccomp filter: " + yn(self.seccomp_works, "works", f"not usable ({self.seccomp_reason or 'unknown'})"))
            out.append("cgroup v2: " + yn(self.cgroup_v2, "present (not used yet)", "not present (not used yet)"))
        elif self.platform == "darwin":
            out.append("sandbox-exec: " + yn(bool(self.sandbox_exec_path), f"found at {self.sandbox_exec_path}; Muse's profile for it is UNTESTED "
                                             "and does not count as isolation", "not found"))
        else:
            out.append("no isolation is available on this system yet")
        out.append("files: " + yn(self.fs_isolation, "a skill can be kept to the folders it was given", "NOT isolated"))
        out.append("network: " + yn(self.net_isolation, "a skill can be cut off completely", "NOT blocked"))
        if self.running_as_root:
            out.append("warning: running as root; the process-count limit is not enforced for root, and root should not run skills")
        return out


@dataclass
class Report:
    """What isolation one run really had. Filled from what the child process reported after locking itself down."""

    mode: str = "off"                   # off | auto | require
    ran: str = "in-process"             # in-process | sandboxed | refused
    reason: str = ""                    # why it ran in-process or was refused
    platform: str = ""
    separate_process: bool = False
    env_scrubbed: bool = False
    bwrap: bool = False
    landlock_abi: int = 0               # the ABI of the ruleset that was applied; 0 = Landlock not applied
    landlock_full: bool = False         # every right the policy wants is handled at that ABI
    seccomp: bool = False
    no_new_privs: bool = False
    caps_dropped: bool = False
    rlimits: dict[str, int] = field(default_factory=dict)
    nproc_enforced: bool = False
    net: str = "not blocked"            # blocked | tcp blocked | not blocked
    files: str = "not isolated"         # isolated | best effort | not isolated
    best_effort: list[str] = field(default_factory=list)  # what a layer could not do here; never empty silently
    stopped: str = ""                   # timeout | data-cap | protocol | crashed | "" (how the sandbox ended the run, if it did)

    @property
    def isolated(self) -> bool:
        """True only when files AND network were both really isolated."""
        return self.ran == "sandboxed" and self.files == "isolated" and self.net == "blocked"

    def layers(self) -> list[str]:
        out: list[str] = []
        if self.ran != "sandboxed":
            return out
        if self.bwrap:
            out.append("bwrap")
        if self.landlock_abi:
            out.append(f"landlock: abi {self.landlock_abi}" + ("" if self.landlock_full else " (best effort)"))
        if self.seccomp:
            out.append("seccomp")
        if self.no_new_privs:
            out.append("no_new_privs")
        if self.rlimits:
            out.append("rlimits")
        out.append(f"files: {self.files}")
        out.append(f"net: {self.net}")
        return out

    def sentence(self) -> str:
        if self.ran == "refused":
            return f"Not run: {self.reason}"
        if self.ran != "sandboxed":
            return "Ran without isolation" + (f" ({self.reason})." if self.reason else ".")
        head = "Ran isolated" if self.isolated else "Ran in a separate process, NOT fully isolated"
        return f"{head}: {', '.join(self.layers())}."

    def public(self) -> dict[str, Any]:
        return {"mode": self.mode, "ran": self.ran, "isolated": self.isolated, "reason": self.reason, "layers": self.layers(),
                "platform": self.platform, "separate_process": self.separate_process, "env_scrubbed": self.env_scrubbed,
                "bwrap": self.bwrap, "landlock_abi": self.landlock_abi, "landlock_full": self.landlock_full,
                "seccomp": self.seccomp, "no_new_privs": self.no_new_privs, "caps_dropped": self.caps_dropped,
                "rlimits": dict(self.rlimits), "nproc_enforced": self.nproc_enforced, "net": self.net, "files": self.files,
                "best_effort": list(self.best_effort), "stopped": self.stopped}


def normalise_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower() or "off"
    if mode not in MODES:
        raise ValueError(f"the sandbox setting must be one of {', '.join(MODES)} (got {str(value)[:20]!r})")
    return mode


def unsafe_home_skills() -> bool:
    return os.environ.get(ENV_UNSAFE_HOME_SKILLS, "") == UNSAFE_HOME_SKILLS_VALUE


def mode_for(source: str, requested: str | None, env: Mapping[str, str]) -> str:
    """The mode a skill runs under.

    Shipped skills follow the setting (`requested`, else MUSE_SANDBOX, else `off`). A skill from anywhere else
    is untrusted code and always runs with `require`, whatever the setting says.
    """
    setting = normalise_mode(requested if requested is not None else env.get(ENV_MODE))
    if source != "shipped" and not unsafe_home_skills():
        return "require"
    return setting
