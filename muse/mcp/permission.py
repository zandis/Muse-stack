"""What the `check_permission` tool answers from.

1. The host's own permission, when the person gave it one (`muse hosts setup --permission`, or a request the
   person approved): `from_home` asks the home, which checks the signed token whose holder is this host's
   device (muse/compiler/hosts.py). The answer is `allow`, `ask` or `deny`.

2. THE INTERIM RULE, clearly labelled as such in every answer (`basis: "interim rule"`), only when the host has
   no permission: `decide` below. Its answer is advice to the host bot, not enforcement:

  * a read-only action on something the person listed when starting the server
    (`--allow-read PATH`, or MUSE_MCP_ALLOW_READ)            -> "allow"
  * anything else, and everything that writes, sends, runs or spends   -> "ask"

The interim rule never answers "deny": with no permission to consult it cannot know that the
person forbade something.

Either way the answer about a host's OWN tools (its shell, its file access) is advice: Muse cannot stop
those. What Muse does enforce for a host with a permission is its model use (its app key stops working when
the permission is stopped or ends) and the skills it runs through Muse.

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
BASIS_PERMISSION = "permission"
BASIS_INTERIM = "interim rule"


def from_home(home: Any, action: str, resource: Any = None, amount: Any = None) -> dict[str, Any] | None:
    """The host's own permission's answer, asked of the home: {"decision", "reason"}; None when the host has no
    permission, or the home cannot say (not running, an older home, no permissions library there)."""
    body: dict[str, Any] = {"action": action}
    if isinstance(resource, str):
        body["resource"] = resource
    if isinstance(amount, dict):
        body["amount"] = amount
    try:
        code, payload = home.call("POST", "/v1/mandates/check", body,
                                  also_ok=("unavailable", "not_found", "forbidden", "not_built", "invalid_request", "internal"))
    except Exception:  # ToolError: the home is not reachable; the interim rule still answers
        return None
    if code != "ok" or payload.get("has_mandate") is not True or payload.get("decision") not in ("allow", "ask", "deny"):
        return None
    return {"decision": payload["decision"], "reason": str(payload.get("reason_words") or payload.get("reason") or "")[:300]}


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
