"""`muse skills ...`: list, show, approve, run. Called from the skills block in muse/cli.py.

Exit codes of `run`: 0 done, 3 partial (something could not be done; the notes
say what), 4 a choice is needed (the choices the router offered are listed),
1 failed, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Mapping

from muse.skills import loader, runner
from muse.skills.format import PRIVACY, TIERS, Skill, needs_in_words

STATE_WORDS = {"ready": "ready", "not-approved": "not approved", "changed": "changed since approval"}
CAVEAT = ("Until Muse's sandbox is built, this list is what the skill declares and what Muse checks on the paths it "
          "hands out. A skill is Python code that runs as you, so approve only code you trust.")


def _say(text: str = "") -> None:
    print(text, file=sys.stderr)


def _describe(skill: Skill, env: Mapping[str, str], out: Callable[[str], None]) -> None:
    source = "ships with Muse" if skill.source == "shipped" else "your own folder"
    out(f"{skill.name} {skill.version} ({source}; {STATE_WORDS.get(skill.state, skill.state)})")
    if skill.author:
        out(f"Written by: {skill.author}")
    out(skill.description)
    if skill.body:
        out("")
        out(skill.body)
    out("")
    out("What it asks for:")
    for line in needs_in_words(skill, data_dir=str(loader.data_dir(skill.name, env))):
        out(f"  - {line}")
    out(f"Checks its own answers with: {skill.check}")
    out(f"Folder: {skill.path}")
    out(f"Content hash: {skill.hash}")


def cmd_list(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    skills, broken = loader.discover(env)
    if args.json:
        print(json.dumps({"skills": [s.public() for s in skills.values()],
                          "broken": [{"name": b.name, "path": b.path, "source": b.source, "problems": list(b.problems)} for b in broken]},
                         indent=2, ensure_ascii=False))
        return 1 if broken else 0
    if not skills and not broken:
        print("No skills found.")
    for skill in sorted(skills.values(), key=lambda s: s.name):
        where = "muse" if skill.source == "shipped" else "yours"
        print(f"{skill.name:<24} {skill.version:<8} {where:<6} {STATE_WORDS.get(skill.state, skill.state):<22} {skill.description[:70]}")
    for item in broken:
        print(f"{item.name:<24} {'':<8} {'muse' if item.source == 'shipped' else 'yours':<6} {'cannot be loaded':<22} {item.problems[0]}")
        for problem in item.problems[1:]:
            print(f"{'':<63}{problem}")
    return 1 if broken else 0


def cmd_show(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    try:
        skill = loader.find(args.name, env)
    except loader.LoaderError as exc:
        _say(f"Error: {exc}")
        return 1
    if args.json:
        print(json.dumps({**skill.public(), "needs_in_words": needs_in_words(skill), "body": skill.body}, indent=2, ensure_ascii=False))
        return 0
    _describe(skill, env, print)
    return 0


def cmd_approve(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    try:
        skill = loader.find(args.name, env)
    except loader.LoaderError as exc:
        _say(f"Error: {exc}")
        return 1
    if skill.source == "shipped":
        print(f"{skill.name} ships with Muse and needs no approval.")
        return 0
    _describe(skill, env, print)
    print("")
    print(CAVEAT)
    if skill.state == "ready":
        print("Already approved, and unchanged since.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            _say("Not approved: answer on a terminal, or pass --yes after reading the list above.")
            return 1
        try:
            reply = input(f"Approve {skill.name}? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Not approved.")
            return 1
    try:
        loader.approve(skill.name, env, expect_hash=skill.hash)
    except loader.LoaderError as exc:
        _say(f"Error: {exc}")
        return 1
    print(f"Approved {skill.name}. It will be refused again if any of its files change.")
    return 0


def _inputs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--input needs key=value, not {pair!r}")
        if value == "-":
            value = sys.stdin.read()  # keeps the text out of shell history
        out[key.strip()] = value
    return out


def grey_line(used: Mapping[str, Any]) -> str:
    """The one line under an answer: who answered and what it cost."""
    providers = [p for p in used.get("providers", {}) if p and p != "unknown"]
    cost = float(used.get("cost_usd") or 0)
    spent = "Nothing spent." if cost <= 0 else f"Cost about US${cost:.4f}."
    if not used.get("requests"):
        return f"No model was used. {spent}"
    if not providers:
        return f"Answered through Muse's router. {spent}"
    if providers == ["cache"]:
        return f"Answered from Muse's own cache. {spent}"
    names = [p for p in providers if p != "cache"]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"Answered by {who}. {spent}"


def option_lines(options: list[Mapping[str, Any]]) -> list[str]:
    lines = []
    for option in options:
        if option.get("choice") == "wait":
            lines.append(f"wait until {option.get('until')}, then run this again")
        elif option.get("choice") == "handoff":
            lines.append("hand off: open your own ChatGPT, Claude or Gemini app with the question")
        elif option.get("choice") == "pay":
            about = float(option.get("about_usd") or 0)
            lines.append(f"pay about US${about:.4f}: run this again with --pay {max(about * 1.5, 0.01):.2f}")
    return lines


def _print_output(output: Any) -> None:
    if output is None:
        return
    if isinstance(output, Mapping) and isinstance(output.get("text"), str):
        print(output["text"])
    elif isinstance(output, str):
        print(output)
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))


def cmd_run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    try:
        inputs = _inputs(args.input or [])
    except ValueError as exc:
        _say(f"Error: {exc}")
        return 2
    try:
        skill = loader.find(args.name, env)
        loader.require_ready(skill)
    except loader.LoaderError as exc:
        _say(f"Error: {exc}")
        return 1
    if not args.json and loader.first_use(skill, env):
        _describe(skill, env, _say)  # the description and what it asks for, once, before first use
        _say("")
    budget = {"paid_usd": args.pay} if args.pay is not None else None
    result = runner.run(skill, inputs, budget=budget, privacy=args.privacy, folders=args.folder or [], out_dir=args.out,
                        task=args.task, environ=env)
    if result.writes and result.status in ("done", "partial"):
        agreed = bool(args.yes)
        if not agreed and not args.json and sys.stdin.isatty():
            _say(f"{skill.name} wants to write {len(result.writes)} new file(s):")
            for write in result.writes:
                _say(f"  {write.path} ({len(write.data)} bytes){': ' + write.reason if write.reason else ''}")
            try:
                agreed = input("Write them? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                agreed = False
        if agreed:
            runner.apply_writes(result)
        else:
            result.notes.append(f"{len(result.writes)} file(s) were proposed and not written. Run again with --yes to write them.")
    if args.json:
        print(json.dumps(result.public(), indent=2, ensure_ascii=False, default=str))
        return result.exit_code
    _print_output(result.output)
    if result.check.get("status") == "failed":
        _say("NOT CHECKED: this answer did not pass the skill's own check. Treat it as a draft.")
    for write in result.writes:
        if write.applied:
            _say(f"Wrote {write.path}")
    _say(grey_line(result.used))
    for note in result.notes:
        _say(f"Note: {note}")
    if result.status == "needs_choice":
        lines = option_lines(result.options)
        if lines:
            _say("Muse could not finish this on free allowance right now. You can:")
            for line in lines:
                _say(f"  - {line}")
        else:
            _say("Muse could not finish this, and nothing else is on offer right now.")
    return result.exit_code


def register(sub: Any, env: Callable[[], Mapping[str, str]]) -> None:
    """Add `muse skills` to the top-level parser. `env` returns the merged environment when a command runs."""
    p = sub.add_parser("skills", help="packaged abilities the helper can use: list, show, approve, run")
    ssub = p.add_subparsers(dest="skills_cmd", metavar="list|show|approve|run|folders")

    def bind(parser: argparse.ArgumentParser, fn: Callable[[argparse.Namespace, Mapping[str, str]], int]) -> None:
        parser.set_defaults(func=lambda args: fn(args, env()))

    ls = ssub.add_parser("list", help="every skill and whether it may run")
    ls.add_argument("--json", action="store_true")
    bind(ls, cmd_list)
    sh = ssub.add_parser("show", help="what a skill does and what it asks for, in plain words")
    sh.add_argument("name")
    sh.add_argument("--json", action="store_true")
    bind(sh, cmd_show)
    ap = ssub.add_parser("approve", help="read what one of your own skills asks for, then allow it to run")
    ap.add_argument("name")
    ap.add_argument("--yes", action="store_true", help="approve without the question (after reading the list)")
    bind(ap, cmd_approve)
    rn = ssub.add_parser("run", help="run a skill once (exit 0 done, 3 partial, 4 needs a choice, 1 failed)")
    rn.add_argument("name")
    rn.add_argument("--input", action="append", metavar="KEY=VALUE", help='an input for the skill; "KEY=-" reads the value from stdin')
    rn.add_argument("--folder", action="append", metavar="PATH", help="a folder the skill may read this time (repeatable)")
    rn.add_argument("--out", metavar="PATH", help="the folder new files may be proposed in (skills that write)")
    rn.add_argument("--task", choices=TIERS, help="how strong a model its steps use by default (see `muse rules`)")
    rn.add_argument("--privacy", choices=("public",) + PRIVACY, help="the privacy class for everything it sends")
    rn.add_argument("--pay", type=float, metavar="USD", help="let this one run spend up to this much (default: nothing)")
    rn.add_argument("--yes", action="store_true", help="write the files it proposes without asking")
    rn.add_argument("--json", action="store_true", help="print the whole result as JSON")
    bind(rn, cmd_run)
    # `muse skills folders add|list|rm`: the folders a skill may be given when run through the home API
    # (by a host bot, say). The list and its commands live with the home: muse/home/folders.py.
    from muse.home import folders as home_folders

    home_folders.register_cli(ssub, bind)

    def usage(args: argparse.Namespace) -> int:
        p.print_help(sys.stderr)
        return 2

    p.set_defaults(func=usage)
