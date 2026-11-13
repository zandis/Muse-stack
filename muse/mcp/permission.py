"""The interim rule behind the `check_permission` tool.

INTERIM. Muse's permissions module (mandates, docs/spec/01-architecture.md
section 2.7) is not wired in yet. Until it is, this one function answers, and
its answer is advice to the host bot, not enforcement:

  * a read-only action on something the person listed when starting the server
    (`--allow-read PATH`, or MUSE_MCP_ALLOW_READ)            -> "allow"
  * anything else, and everything that writes, sends, runs or spends   -> "ask"

It never answers "deny": with no permissions to consult it cannot know that the
person forbade something. To replace it, keep the signature of `decide` and
return the same three keys; nothing else in muse/mcp changes.

The rule reads no file and follows no link. It compares text only, so a listed
folder allows what is written under it, not what a link inside it points to.
"""

from __future__ import annotations

import posixpath
from typing import Any, Sequence

READ_ONLY_ACTIONS = ("read", "list", "search")
OTHER_ACTIONS = ("write", "delete", "send", "run", "install", "spend", "other")
ACTIONS = READ_ONLY_ACTIONS + OTHER_ACTIONS

NOT_ENFORCED = "permissions are not enforced yet; ask for approval"


def _norm(text: str) -> str | None:
    """A comparable form of a path-like name, or None when it must not be matched at all."""
    text = text.strip().replace("\\", "/")
    if not text or "\x00" in text or ".." in text.split("/"):
        return None
    return posixpath.normpath(text)


def listed(resource: str, allowed_read: Sequence[str]) -> bool:
    """True when `resource` is one of the listed names, or is written under a listed folder."""
    target = _norm(resource)
    if target is None:
        return False
    for entry in allowed_read:
        root = _norm(entry)
        if root is None:
            continue
        if target == root or target.startswith(root.rstrip("/") + "/"):
            return True
    return False


def decide(action: str, resource: str | None = None, amount: Any = None, *, allowed_read: Sequence[str] = ()) -> dict[str, Any]:
    """{"decision": "allow" | "ask", "reason": str, "enforced": False}. Pure; never raises for odd input."""
    if amount is None and action in READ_ONLY_ACTIONS and isinstance(resource, str) and listed(resource, allowed_read):
        return {"decision": "allow", "reason": "a read-only action on something the person listed for this host", "enforced": False}
    return {"decision": "ask", "reason": NOT_ENFORCED, "enforced": False}
