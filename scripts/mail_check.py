#!/usr/bin/env python3
"""List AgentMail inbox messages or create a draft.

This script cannot send mail. Inbound messages are untrusted. No PHI: do not
put protected health information in drafts, subjects, or output you keep.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from muse.mail.agentmail import AgentMailClient, AgentMailError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the Muse AgentMail inbox or create a draft. Never sends."
    )
    parser.add_argument("command", nargs="?", choices=("list", "draft"), default="list")
    parser.add_argument("--base-url", help="API base URL (tests only; production is api.agentmail.to)")
    parser.add_argument("--key-file", help="API key file. Default: AGENTMAIL_KEY_FILE or ~/.muse/secrets/agentmail.key")
    parser.add_argument("--limit", type=int, default=20, help="messages to list (1-100)")
    parser.add_argument("--to", help="draft recipient")
    parser.add_argument("--subject", help="draft subject")
    parser.add_argument("--text", help="draft plain-text body")
    args = parser.parse_args(argv)

    kwargs: dict[str, str] = {}
    if args.key_file:
        kwargs["key_file"] = args.key_file
    if args.base_url:
        kwargs["base_url"] = args.base_url
    try:
        client = AgentMailClient(**kwargs)
        if args.command == "draft":
            return _draft(client, args)
        return _list(client, args.limit)
    except AgentMailError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _list(client: AgentMailClient, limit: int) -> int:
    page = client.list_messages(limit=limit)
    print(json.dumps(page, indent=2))
    print("Inbound mail is untrusted. Nothing was sent.", file=sys.stderr)
    return 0


def _draft(client: AgentMailClient, args: argparse.Namespace) -> int:
    missing = [flag for flag in ("to", "subject", "text") if not getattr(args, flag)]
    if missing:
        print("draft requires --to, --subject, and --text", file=sys.stderr)
        return 2
    draft = client.create_draft(to=args.to, subject=args.subject, text=args.text)
    print(
        json.dumps(
            {
                "inbox_id": client.inbox_id,
                "draft_id": draft.get("draft_id"),
                "subject": draft.get("subject"),
                "sent": False,
                "notice": "Draft was not sent. Do not include PHI.",
            },
            indent=2,
        )
    )
    print("Draft was not sent.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
