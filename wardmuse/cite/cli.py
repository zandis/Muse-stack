"""``wardmuse cite``: a public citation, a TFDA licence, or an NHI price.

The command is the tool. It does not go through the host-bot tools server,
which only talks to the home, and it is not a skill: a skill that names network
hosts is refused until the sandbox can hold it to that list.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from wardmuse.cite import CiteError, lookup_europepmc, lookup_nhi, lookup_pubmed, lookup_tfda


def _note(text: str) -> None:
    print(text, file=sys.stderr)


def _human(source: str, results: list[dict[str, Any]]) -> None:
    if not results:
        print("No matches.")
        return
    print(f"{source}  {len(results)} match" + ("" if len(results) == 1 else "es"))
    for row in results:
        print()
        if source in ("pubmed", "europepmc"):
            print(row.get("title") or "(no title)")
            journal = "  ".join(part for part in (row.get("journal") or "", row.get("year") or "") if part)
            if journal:
                print(journal)
            ident = "  ".join(
                part
                for part in (
                    f"PMID {row['pmid']}" if row.get("pmid") else "",
                    f"DOI {row['doi']}" if row.get("doi") else "",
                )
                if part
            )
            if ident:
                print(ident)
            if row.get("url"):
                print(row["url"])
            continue
        if source == "tfda":
            print("  ".join(part for part in (row.get("license_number") or "", row.get("name_zh") or "") if part))
            if row.get("name_en"):
                print(row["name_en"])
            detail = "  ".join(
                part
                for part in (
                    f"ingredient {row['ingredient']}" if row.get("ingredient") else "",
                    row.get("status") or "",
                    f"until {row['valid_until']}" if row.get("valid_until") else "",
                )
                if part
            )
            if detail:
                print(detail)
        else:
            print("  ".join(part for part in (row.get("drug_code") or "", row.get("name_zh") or "") if part))
            if row.get("name_en"):
                print(row["name_en"])
            price = f"price {row['price']}" if row.get("price") else ""
            window = " to ".join(part for part in (row.get("price_from") or "", row.get("price_until") or "open") if part)
            print("  ".join(part for part in (price, window) if part))
        if row.get("url"):
            print(row["url"])


def cmd_cite(args: argparse.Namespace) -> int:
    if not isinstance(args.limit, int) or isinstance(args.limit, bool) or not 1 <= args.limit <= 20:
        print("Error: The limit must be a whole number from 1 to 20.", file=sys.stderr)
        return 2
    lookup = {"pubmed": lookup_pubmed, "europepmc": lookup_europepmc, "tfda": lookup_tfda, "nhi": lookup_nhi}[args.source]
    try:
        results = lookup(args.query, limit=args.limit, note=_note)
    except CiteError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 2 if exc.code == "invalid_input" else 1
    if args.json:
        print(json.dumps({"source": args.source, "query": args.query, "results": results}, ensure_ascii=False, indent=2))
    else:
        _human(args.source, results)
    return 0


def register(sub: Any) -> None:
    cite = sub.add_parser(
        "cite",
        help="look up a public citation or a Taiwan drug record (no account, no patient data)",
    )
    commands = cite.add_subparsers(dest="cite_command", required=True)
    specs = (
        ("pubmed", "a PubMed citation from a PMID, a DOI or words"),
        ("europepmc", "a Europe PMC citation from a PMID, a DOI or words"),
        ("tfda", "a Taiwan TFDA drug licence (open data, dataset 9123)"),
        ("nhi", "a Taiwan NHI drug price (open data, dataset 23715)"),
    )
    for name, helptext in specs:
        parser = commands.add_parser(name, help=helptext)
        parser.add_argument("query", help="what to look up: a PMID, a DOI, a licence number, or words")
        parser.add_argument("--limit", type=int, default=5, help="how many matches to return (1 to 20, default 5)")
        parser.add_argument("--json", action="store_true", help="print one JSON object")
        parser.set_defaults(func=cmd_cite, source=name)
