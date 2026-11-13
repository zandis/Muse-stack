"""`muse mcp serve` and `muse hosts list|setup|show|check`.

    muse mcp serve --token-file PATH       the tools server on stdin/stdout (same as python -m muse.mcp)
    muse hosts list                        the hosts Muse has settings for, and how each was checked
    muse hosts setup openmausbot           create its app and tools token, print what to paste
    muse hosts show openmausbot            print what to paste again (creates nothing, shows no secret)
    muse hosts check openmausbot FILE      read the host's configuration and say what goes through Muse

Kept out of muse/cli.py so that file only carries a small registration block.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Mapping


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from muse.mcp import server

    return server.run(args)


def _hosts_list(args: argparse.Namespace) -> int:
    from muse import presets

    if args.json:
        print(json.dumps([{"name": h.kind, "title": h.title, "engine": bool(h.engine), "verified": h.verified,
                           "sources": list(h.sources), "not_verified": list(h.unverified)} for h in presets.HOSTS.values()], indent=2))
        return 0
    for h in presets.HOSTS.values():
        doors = "engine and tools" if h.engine else "tools only"
        checked = f"field names read on its own pages {h.verified}" if h.verified else "UNVERIFIED"
        print(f"{h.kind:<14} {h.title}: {doors}; {checked}; not run here")
    print()
    print("`muse hosts setup <name>` creates the host's own key and prints what to paste into it.")
    return 0


def _hosts_show(args: argparse.Namespace) -> int:
    from muse import presets
    from muse.mcp import hosts

    host = presets.get_host(args.name)
    name = args.app_name or host.kind
    print(hosts.snippet(host, args.base_fn(), hosts.token_file_for(name)))
    return 0


def _hosts_setup(args: argparse.Namespace) -> int:
    from muse import presets
    from muse.mcp import hosts

    host = presets.get_host(args.name)
    if host.engine and not (args.show_token or sys.stdout.isatty()):
        # The same rule as `muse apps`: checked before anything is created.
        _err("Refusing to print a new key: stdout is not a terminal, so it could end up in a log. "
             "Run this in a terminal, or add --show-token if you really want it on stdout. Nothing was created or changed.")
        return 2
    try:
        done = hosts.setup(host, app_name=args.app_name, app_environ=args.env_fn())
    except hosts.SetupError as exc:
        _err(f"Error: {exc}. Nothing was created or changed.")
        return 1
    base = args.base_fn()
    if done.app_token:
        print(f"Created app '{done.app_name}' for {host.title}: {host.priority}, no daily caps, paid: no.")
        print()
        print("  COPY THE KEY NOW. It is shown once. Muse keeps only a hash, so it cannot be shown again.")
        print(f"  If you lose it, `muse apps rotate {done.app_name}` issues a new one and the old one stops working.")
        print()
        print(f"  Muse app key : {done.app_token}")
        print(f"  It goes where the settings below say {presets.KEY_PLACEHOLDER}.")
        print()
    print(f"Created device '{done.device}' for the Muse tools server. Its token is in {done.token_file} (only you can read it);")
    print("it was not printed and is not needed anywhere else.")
    print("That token has the role `host`: it can read status, ask you questions, queue its own jobs, add records and run")
    print("skills. It cannot pause or resume, store keys, manage apps, answer a question or read your records.")
    print()
    print(hosts.snippet(host, base, done.token_file))
    print()
    print("Muse did not touch the host's files. The tools server needs the home running: `muse home serve`.")
    if host.engine:
        print(f"Afterwards, `muse hosts check {host.kind} <path to its config file>` says what goes through Muse.")
    print(f"What is and is not protected under a host bot: docs/hosts/README.md. To undo: `muse apps rm {done.app_name or '<app>'}`"
          f"{'' if done.app_name else ' (no app was made)'}, `muse home token rm {done.device}`, and delete the token file.")
    return 0


def _hosts_check(args: argparse.Namespace) -> int:
    from muse import presets
    from muse.mcp import hosts

    host = presets.get_host(args.name)
    report = hosts.check_config(host, args.config, args.base_fn())
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.text())
        if report.readable and not host.verified:
            print("  UNVERIFIED: this host's file format was not confirmed on its own pages.")
    return report.exit_code


def cmd_hosts(args: argparse.Namespace) -> int:
    func = getattr(args, "hosts_func", None)
    if func is None:
        args.hosts_parser.print_help(sys.stderr)
        return 2
    return int(func(args))


def cmd_mcp(args: argparse.Namespace) -> int:
    func = getattr(args, "mcp_func", None)
    if func is None:
        args.mcp_parser.print_help(sys.stderr)
        return 2
    return int(func(args))


def register(sub: Any, env_fn: Callable[[], Mapping[str, str]], base_fn: Callable[[], str]) -> None:
    from muse import presets
    from muse.mcp import server

    m = sub.add_parser("mcp", help="the Muse tools server for host bots (Model Context Protocol)")
    msub = m.add_subparsers(dest="mcp_cmd")
    m.set_defaults(func=cmd_mcp, mcp_parser=m)
    ms = msub.add_parser("serve", help="run it on stdin/stdout; a host bot starts this, not a person")
    server.add_arguments(ms)
    ms.set_defaults(mcp_func=cmd_mcp_serve)

    h = sub.add_parser("hosts", help="run Muse under an existing bot: settings to paste, and a read-only check")
    hsub = h.add_subparsers(dest="hosts_cmd")
    h.set_defaults(func=cmd_hosts, hosts_parser=h, env_fn=env_fn, base_fn=base_fn)
    names = list(presets.HOSTS)

    hl = hsub.add_parser("list", help="the hosts Muse has settings for")
    hl.add_argument("--json", action="store_true")
    hl.set_defaults(hosts_func=_hosts_list)

    hs = hsub.add_parser("setup", help="create the host's own key and tools token, and print what to paste into the host")
    hs.add_argument("name", choices=names)
    hs.add_argument("--app-name", metavar="NAME", help="name the app something other than the host's name")
    hs.add_argument("--show-token", action="store_true", help="print the app key even when stdout is not a terminal")
    hs.set_defaults(hosts_func=_hosts_setup)

    hw = hsub.add_parser("show", help="print what to paste again; creates nothing and shows no secret")
    hw.add_argument("name", choices=names)
    hw.add_argument("--app-name", metavar="NAME")
    hw.set_defaults(hosts_func=_hosts_show)

    hc = hsub.add_parser("check", help="read the host's configuration file (never changes it) and say what goes through Muse")
    hc.add_argument("name", choices=names)
    hc.add_argument("config", metavar="FILE", help="the host's configuration file, e.g. ~/.openmausbot/config.json")
    hc.add_argument("--json", action="store_true")
    hc.set_defaults(hosts_func=_hosts_check)
