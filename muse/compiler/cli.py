"""`muse permissions grant | stop | status | sweep`: the compiler from the command line.

    muse permissions grant            fields -> the sentence -> your yes -> issued -> compiled -> a mark per target
    muse permissions stop <id>        stop one everywhere; shows each target turning off
    muse permissions status [<id>]    every permission (or one) with its marks, the setting in force, and anything
                                      that breaks the rule "no artifact without a permission"
    muse permissions sweep            remove what stopped or ended permissions left behind; make again what a live
                                      one is missing. Safe to run every minute (a timer on the always-on home).

Called from the `permissions` block in muse/cli.py, which passes its own small parsing helpers (`h`).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Mapping

LABELS = {"skills": "Helper", "router": "Router app", "records": "Records", "payments": "Spending limit",
          "connectors": "Connectors", "health": "Health"}
MARKS = {"active": "✓", "pending": "…", "failed": "✗", "not_built": "– (not built)", "revoked": "off"}


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def marks(targets: Mapping[str, Mapping[str, str]], indent: str = "  ") -> list[str]:
    """`Helper ✓, Records ✓` on one line, then a line for every mark that is not a plain tick."""
    head = ", ".join(f"{LABELS.get(t, t)} {MARKS.get(s['state'], s['state'])}" for t, s in targets.items())
    out = [indent + (head or "nothing to enforce")]
    for target, state in targets.items():
        why = state.get("reason") or state.get("note")
        if why:
            out.append(f"{indent}  {LABELS.get(target, target)}: {why}")
    return out


def _ask(question: str) -> str:
    return input(question).strip()


def _grant(args: argparse.Namespace, env: Mapping[str, str], h: Any) -> int:
    from muse import permissions as P
    from muse.compiler.core import Compiler
    from muse.compiler.plan import plan

    now = h.time(args.at) if getattr(args, "at", None) else int(time.time())
    interactive = sys.stdin.isatty()
    holder, actions, resources = args.holder, list(args.action or ()), list(args.resource or ())
    if not (holder and actions and resources):
        if not interactive:
            _err("Error: give --holder, --action and --resource, or run this in a terminal to be asked for them")
            return 2
        print("A permission says who may do what, with what, until when. You will see it as a sentence before anything is signed.")
        holder = holder or _ask("Who is it for? (helper:home, app:reader, device:phone-1) ")
        actions = actions or [a for a in _ask("What may they do? (files.read, files.write, llm.call, llm.paid; commas between) ").replace(" ", "").split(",") if a]
        resources = resources or [r.strip() for r in _ask("With what? (folder:/home/you/Notes, provider:router; commas between) ").split(",") if r.strip()]
        days = _ask(f"For how many days? [{args.days}] ")
        if days:
            try:
                args.days = int(days)
            except ValueError:
                _err("Error: days must be a whole number")
                return 2
    compiler = Compiler(env)
    if not compiler.keys.exists():
        print(f"Created a root key at {compiler.keys.path} (keep that file safe: it signs every permission).")
    issuer = compiler.ensure_root()
    limit = {3: lambda kind, period, n: P.Limit(kind, "", 0, period, int(n)),
             5: lambda kind, unit, dec, period, n: P.Limit(kind, unit, int(dec), period, int(n))}
    start = h.time(args.start) if args.start else now
    draft = P.propose_from_fields(P.Draft(
        issuer=issuer, holder=holder, actions=tuple(actions), routine=tuple(args.routine or ()),
        resources=tuple(h.resource(r) for r in resources),
        limits=tuple(h.fields(l, limit, "limit", "spend:USD:2:day:200 or requests:day:200") for l in args.limit or ()),
        purpose_text=args.purpose, purpose_code=args.purpose_code, issued_at=now, not_before=start,
        not_after=h.time(args.until) if args.until else start + 86400 * args.days,
        confirm=h.confirm(args.confirm), privacy=args.privacy, period_zone=args.zone))
    shown = P.Mandate(biscuit=None, id=P.mandate_id(draft), draft=draft)  # nothing is signed yet
    h.show(shown, args.lang)
    specs = plan(shown)
    makes_app = any(s.kind == "app" for s in specs)
    print("This will set up: " + ", ".join(dict.fromkeys(
        LABELS.get(s.target, s.target) + (" (not built yet)" if s.status == "not_built" else "") for s in specs)))
    if makes_app and not (args.show_token or sys.stdout.isatty()):
        _err("Refusing to go on: this permission makes an app key, which is shown once, and stdout is not a terminal, so it "
             "could end up in a log. Run this in a terminal, or add --show-token. Nothing was issued.")
        return 2
    if not h.yes(args, "Issue this permission?"):
        print("Not issued.")
        return 1
    mandate, applied = compiler.issue(draft)
    print(f"Issued {mandate.id[3:15]} (id {mandate.id})")
    for line in marks(applied.targets):
        print(line)
    if applied.app_token:
        print()
        print(f"  COPY THE KEY NOW. It is shown once. It is the key of the router app '{applied.app_name}', made from this permission,")
        print("  and it stops working the moment the permission is stopped or ends.")
        print(f"  Muse app key : {applied.app_token}")
    return 0 if all(t["state"] in ("active", "not_built") for t in applied.targets.values()) else 3


def _stop(args: argparse.Namespace, env: Mapping[str, str], h: Any) -> int:
    from muse.compiler.core import Compiler

    compiler = Compiler(env)
    mandate = compiler.find(args.name)
    if mandate is None:
        _err(f"Error: no one permission starts with {args.name!r} (see `muse permissions status`)")
        return 1
    h.show(mandate, args.lang)
    if compiler.state_of(mandate) != "stopped" and not h.yes(args, "Stop this permission and everything made from it?"):
        print("Not stopped.")
        return 1
    targets = compiler.revoke(mandate.id)
    print(f"Stopped {mandate.id[3:15]}.")
    for line in marks(targets):
        print(line)
    done = all(t["state"] in ("revoked", "not_built") for t in targets.values())
    if not done:
        print("  Some parts could not be removed yet. `muse permissions sweep` tries again; nothing made from this permission "
              "is honoured meanwhile.")
    return 0 if done else 3


def _status(args: argparse.Namespace, env: Mapping[str, str], h: Any) -> int:
    from muse import permissions as P
    from muse.compiler import guard
    from muse.compiler.core import Compiler

    if not P.available():
        print("Permissions need the `biscuit-python` package on this computer (pip install 'muse-stack[permissions]'). Until then")
        print("nothing is checked: skills run as they always did and no router app is made from a permission.")
        return 0
    compiler = Compiler(env)
    found = compiler.mandates()
    if args.name:
        one = compiler.find(args.name)
        if one is None:
            _err(f"Error: no one permission starts with {args.name!r}")
            return 1
        found = [one]
    try:
        mode = guard.normalise_mode(env.get(guard.ENV_MODE))
    except ValueError as exc:
        _err(f"Error: {exc}")
        return 2
    rows = [{"id": m.id, "name": m.id[3:15], "state": compiler.state_of(m), "holder": m.draft.holder,
             "sentence": P.describe(m, args.lang).sentence, "until": P.format_time(m.not_after()),
             "targets": compiler.status(m.id)} for m in found]
    orphans = compiler.orphans()
    if args.json:
        print(json.dumps({"permissions": rows, "skills": mode, "orphans": orphans}, ensure_ascii=False, indent=2))
        return 0 if not orphans else 3
    if not rows:
        print("No permissions yet. `muse permissions grant` gives one.")
    for row in rows:
        print(f"{row['name']}  [{row['state']}]  until {row['until']}")
        print(f"  {row['sentence']}")
        for line in marks(row["targets"]):
            print(line)
    print()
    print(f"Skills are checked against permissions in '{mode}' mode (MUSE_PERMISSIONS=off|advise|enforce). A skill that does not")
    print("ship with Muse is always 'enforce'. Router apps made from a permission stop when it is stopped or ends.")
    print("Not built: payments, connectors, health, approval with a passkey, sharing the stopped list with other devices.")
    for line in orphans:
        print(f"WARNING  {line} (run `muse permissions sweep`)")
    return 0 if not orphans else 3


def _sweep(args: argparse.Namespace, env: Mapping[str, str], h: Any) -> int:
    from muse.compiler.core import Compiler

    result = Compiler(env).sweep()
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["skipped"]:
        print(f"Nothing was done: {result['skipped']}.")
    else:
        print(f"Cleaned up after {len(result['removed'])} stopped or ended permission(s); made {len(result['applied'])} whole again; "
              f"{result['failed']} thing(s) still wrong.")
    return 0 if not result["failed"] and not result["skipped"] else 3


def run(args: argparse.Namespace, env: Mapping[str, str], h: Any) -> int:
    return {"grant": _grant, "stop": _stop, "status": _status, "sweep": _sweep}[args.permissions_cmd](args, env, h)


def add_parsers(add: Any) -> None:
    """Register the four commands. `add(name, help)` is the block's own helper (it adds --lang and --at)."""
    g = add("grant", "give a permission: read it as a sentence, sign it once you agree, and set up what enforces it")
    g.add_argument("--holder", help="who may use it, e.g. helper:home, app:reader, device:phone-1 (asked for when left out)")
    g.add_argument("--action", action="append", help="e.g. files.read, files.write, llm.call, llm.paid (repeat)")
    g.add_argument("--resource", action="append", help="kind:value, e.g. folder:/home/you/Notes, provider:router, home:skill/files-answer (repeat)")
    g.add_argument("--limit", action="append", help="spend:USD:2:day:200 (unit, decimals, period, minor units) or requests:day:200 (repeat)")
    g.add_argument("--routine", action="append", help="an action that need not be confirmed each time (repeat)")
    g.add_argument("--purpose", default="", help="why, in your own words")
    g.add_argument("--purpose-code", default="", help="a purpose code; router.priority.background sets a router app's priority")
    g.add_argument("--from", dest="start", metavar="TIME", help="start (default: now)")
    g.add_argument("--until", metavar="TIME", help="end (default: --days after the start)")
    g.add_argument("--days", type=int, default=30, help="how long it lasts when --until is not given (default 30)")
    g.add_argument("--confirm", default="each_write", help="each_write (default), never, or over_amount:USD:2:500")
    g.add_argument("--privacy", default="private", choices=["local", "private", "standard", "public"],
                   help="least protective class it may send data under (default private)")
    g.add_argument("--zone", default="UTC", help="time zone days and months are counted in (default UTC)")
    g.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    g.add_argument("--show-token", action="store_true", help="print a new app key even when stdout is not a terminal")
    s = add("stop", "stop a permission everywhere: the stopped list first, then everything made from it")
    s.add_argument("name", help="the permission's short name or the start of its id")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    st = add("status", "every permission with a mark per enforcing system, and what is enforced under which setting")
    st.add_argument("name", nargs="?", help="one permission (default: all)")
    st.add_argument("--json", action="store_true")
    sw = add("sweep", "remove what stopped or ended permissions left behind; safe to run every minute")
    sw.add_argument("--json", action="store_true")
