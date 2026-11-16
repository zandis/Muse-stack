"""`python -m muse.home`: run the home control API, or the fake home with --fake."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from muse.home import fake, server

    p = argparse.ArgumentParser(prog="python -m muse.home", description="The home control API (docs/spec/interfaces/home-api.v1.yaml).")
    p.add_argument("--fake", action="store_true", help="serve canned data for app development: no proxy, ledger or keys needed")
    p.add_argument("--state", choices=fake.STATES, default="normal", help="with --fake: which situation to show (default normal)")
    p.add_argument("--tcp", type=int, metavar="PORT", help="listen on 127.0.0.1:PORT instead of a Unix socket (0 picks a free port)")
    p.add_argument("--socket", metavar="PATH", help="Unix socket path (default MUSE_HOME/home.sock; a temporary folder with --fake)")
    args = p.parse_args(argv)
    if args.state != "normal" and not args.fake:
        p.error("--state only applies with --fake")
    home = None
    try:
        if args.fake:
            home = fake.FakeHome(args.state)
            sock = args.socket or (None if args.tcp is not None else home.root / server.SOCKET_NAME)
            server.accept_stop_requests()
            srv = server.make_server(home, fake.fake_verifier(), socket_path=sock, tcp_port=args.tcp)
            print(f"FAKE home, state '{args.state}'. Canned data; nothing here is real and nothing is kept.")
            print(f"  Token : {fake.FAKE_TOKEN}   (send as  Authorization: Bearer {fake.FAKE_TOKEN})")
            server.serve(srv, banner="  Listen")
        else:
            server.accept_stop_requests()
            srv = server.make_server(socket_path=Path(args.socket) if args.socket else None, tcp_port=args.tcp)
            server.serve(srv)
    except (ValueError, OSError) as exc:
        print(f"Could not start the home: {exc}", file=sys.stderr)
        return 1
    finally:
        if home is not None:
            home.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
