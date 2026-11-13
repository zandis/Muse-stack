"""The skill sandbox: run a skill's code in a separate, locked-down process (docs/sandbox.md).

    result, report = run_skill("files-answer", {"question": "..."}, roots=["~/notes"], mode="require")
    report.isolated      True only when files AND network were really isolated
    report.layers()      ["bwrap", "landlock: abi 7", "seccomp", "no_new_privs", "rlimits", "files: isolated", "net: blocked"]

    probe()              what this machine can enforce (for `muse doctor`)

The parent keeps the router client, the budget and the privacy class; the
child gets no key, no token, no environment and no network, and reaches a
model only by asking the parent over a pipe. This has NOT had an outside
security review. Until it has, treat it as a strong seat belt, not a vault.

Names are loaded on first use so that the child process and `muse doctor`
import only what they need.
"""

from __future__ import annotations

from typing import Any

from muse.sandbox.policy import MODES, Capabilities, Policy, Report, mode_for, normalise_mode  # noqa: F401

__all__ = ["MODES", "Capabilities", "Policy", "Report", "mode_for", "normalise_mode", "probe", "run_skill", "Executor", "SandboxStop"]


def __getattr__(name: str) -> Any:
    if name == "probe":
        from muse.sandbox.detect import probe
        return probe
    if name in ("run_skill", "Executor", "SandboxStop", "in_process_reason"):
        from muse.sandbox import host
        return getattr(host, name)
    raise AttributeError(name)
