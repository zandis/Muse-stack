"""`muse home ...`: run the control API, manage device tokens, and call it by hand.

    muse home serve [--tcp PORT]
    muse home token add|list|rm|rotate
    muse home call GET /v1/status

Kept out of muse/cli.py so that file only carries a small registration block.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

TOKEN_ENV = "MUSE_HOME_TOKEN"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _token_display_ok(args: argparse.Namespace) -> bool:
    """The same rule as `muse apps`: a new token is printed only to a terminal, or with --show-token.

    Checked before anything is created, so a refusal never leaves a token nobody saw.
    """
    if getattr(args, "show_token", False) or sys.stdout.isatty():
        return True
    _err(
        "Refusing to print a new device token: stdout is not a terminal, so it could end up in a log. "
        "Run this in a terminal, or add --show-token if you really want it on stdout. Nothing was created or changed."
    )
    return False


def _print_token(name: str, token: str) -> None:
    print()
    print("  COPY THE TOKEN NOW. It is shown once. Muse keeps only a hash, so it cannot be shown again.")
    print(f"  If you lose it, `muse home token rotate {name}` issues a new one and the old one stops working.")
    print()
    print(f"  Device token : {token}")
    print()
    print("  The device sends it with every request as  Authorization: Bearer <token>.")


def cmd_serve(args: argparse.Namespace) -> int:
    from muse.home import auth, server

    try:
        srv = server.make_server(socket_path=Path(args.socket) if args.socket else None, tcp_port=args.tcp)
    except (ValueError, OSError) as exc:
        _err(f"Could not start the home: {exc}")
        return 1
    if not auth.list_devices():
        print("No device can sign in yet. Create a token with `muse home token add <name>`.")
    if srv.is_tcp:
        print("Listening on 127.0.0.1: any program on this computer can reach it, so the device token is the only check.")
    server.serve(srv)
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from muse.home import auth

    try:
        if args.token_cmd == "list":
            devices = auth.list_devices()
            if args.json:
                print(json.dumps([{"name": d.name, "created_at": d.created_at, "rotated_at": d.rotated_at} for d in devices], indent=2))
            elif not devices:
                print("No devices. `muse home token add <name>` creates one.")
            else:
                for d in devices:
                    rotated = f", token rotated {d.rotated_at}" if d.rotated_at else ""
                    print(f"{d.name:<24} added {d.created_at}{rotated}")
            return 0
        if args.token_cmd == "rm":
            if auth.remove_token(args.name):
                print(f"Removed '{args.name}'. Its token no longer works.")
                return 0
            _err(f"Error: no device named '{args.name}'")
            return 1
        if not _token_display_ok(args):
            return 1
        if args.token_cmd == "add":
            device, token = auth.add_token(args.name)
            print(f"Added device '{device.name}'.")
        else:
            device, token = auth.rotate_token(args.name)
            print(f"New token for '{device.name}'. The old one no longer works.")
        _print_token(device.name, token)
        return 0
    except auth.TokenError as exc:
        _err(f"Error: {exc}")
        return 1


def _read_token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    if sys.stdin.isatty():
        return getpass.getpass("Device token (hidden): ").strip() or None
    return None


def _read_body(args: argparse.Namespace) -> Any:
    raw = args.data
    if raw is None:
        return None
    if raw == "-":
        text = sys.stdin.read()
    elif raw.startswith("@"):
        text = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
    else:
        if "/keys/" in args.path:
            raise ValueError("a key must not be typed on the command line; use --data - (stdin) or --data @file")
        text = raw
    try:
        return json.loads(text)
    except ValueError:
        raise ValueError("the body is not valid JSON") from None


def cmd_call(args: argparse.Namespace) -> int:
    from muse.home.client import HomeClient

    try:
        body = _read_body(args)
    except (ValueError, OSError) as exc:
        _err(f"Error: {exc}")
        return 2
    token = _read_token()
    if not token:
        _err(f"Error: no device token. Set {TOKEN_ENV}, or run this in a terminal to be asked for it. "
             "`muse home token add <name>` creates one.")
        return 2
    client = HomeClient(token, socket_path=args.socket, tcp_port=args.tcp)
    try:
        status, payload, _ = client.request(args.method.upper(), args.path, body)
    except OSError as exc:
        _err(f"Error: cannot reach the home ({type(exc).__name__}). Is `muse home serve` running?")
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if status >= 400:
        _err(f"HTTP {status}")
        return 1
    return 0


def cmd_home(args: argparse.Namespace) -> int:
    fn = getattr(args, "home_func", None)
    if fn is None:
        args.home_parser.print_help(sys.stderr)
        return 2
    return int(fn(args))


def register(sub: Any) -> None:
    h = sub.add_parser("home", help="the home control API: serve it, manage device tokens, call it by hand")
    hsub = h.add_subparsers(dest="home_cmd")
    h.set_defaults(func=cmd_home, home_parser=h)

    def where(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--tcp", type=int, metavar="PORT", help="use 127.0.0.1:PORT instead of the Unix socket")
        sp.add_argument("--socket", metavar="PATH", help="Unix socket path (default MUSE_HOME/home.sock)")

    s = hsub.add_parser("serve", help="run the control API (a private Unix socket; --tcp PORT for 127.0.0.1)")
    where(s)
    s.set_defaults(home_func=cmd_serve)

    t = hsub.add_parser("token", help="device tokens: who may use the control API")
    tsub = t.add_subparsers(dest="token_cmd", required=True)
    ta = tsub.add_parser("add", help="create a device and show its token once")
    ta.add_argument("name")
    ta.add_argument("--show-token", action="store_true", help="print the token even when stdout is not a terminal")
    tl = tsub.add_parser("list", help="every device (names and dates; never tokens)")
    tl.add_argument("--json", action="store_true")
    tr = tsub.add_parser("rm", help="remove a device; its token stops working")
    tr.add_argument("name")
    tro = tsub.add_parser("rotate", help="issue a new token; the old one stops working at once")
    tro.add_argument("name")
    tro.add_argument("--show-token", action="store_true", help="print the token even when stdout is not a terminal")
    t.set_defaults(home_func=cmd_token)

    c = hsub.add_parser("call", help=f"send one request, e.g. `muse home call GET /v1/status` (token from {TOKEN_ENV}, or asked for)")
    c.add_argument("method", help="GET, POST, PATCH or DELETE")
    c.add_argument("path", help="e.g. /v1/status")
    c.add_argument("--data", metavar="JSON", help="request body: JSON text, @file, or - for stdin")
    where(c)
    c.set_defaults(home_func=cmd_call)
