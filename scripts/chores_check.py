#!/usr/bin/env python3
"""Run the shipped skills many times on FREE allowances and say how often they pass (gate G1).

Run it on your own machine, after `muse up`. It runs each task in
evals/chores/<skill>/tasks.jsonl through the real skill runner against the local
router, with the runner's own never-paid app (`skills`) and a money budget of 0.

Before anything is sent it prints the plan and the worst case (runs, requests,
tokens) and asks you to confirm unless you pass --yes. It is slow on purpose:
one run at a time, a pause between runs, a short Retry-After is sat out and a
long one records the run as `deferred`. If the router ever reports a cost above
zero it stops at once.

Two scores per run:
  own check     the skill's own check, as the runner reports it (what G1 counts)
  independent   the expected facts in the task file: text that must be in the
                answer and the file it must cite. It shows when the own check
                passed an answer that was wrong.

What it writes:
  $MUSE_HOME/chores-report.json   the report (resume with --resume)
  $MUSE_HOME/chores-runs.csv      one line per run, appended, to compare nights
By default neither holds document text, a prompt, a model's answer or a check
reason's wording: ids, counts, provider names, short codes. --keep-outputs adds
the answers and reasons to the JSON report for debugging.
tests/test_chores_check.py runs this script against a fake router with canary
values and fails if any of them reach the report or the CSV.

Exit code: 0 every skill met the G1 line; 1 one did not, or the router could not
be reached; 2 a usage problem; 3 stopped because something was charged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

TASKS_DIR = ROOT / "evals" / "chores"
FIXTURES = "fixtures"
TASK_FILE = "tasks.jsonl"
G1_RATE = 0.80
G1_RUNS = 50
DEFAULT_DELAY = 2.0
DEFAULT_MAX_WAIT = 60.0
DEFAULT_STOP_AFTER = 5
UNREACHABLE_STOP = 2
Z95 = 1.959963984540054
EXIT_OK, EXIT_NOT_MET, EXIT_USAGE, EXIT_SPENT = 0, 1, 2, 3
CSV_FIELDS = ("time", "run_id", "set", "skill", "version", "task", "kind", "lang", "repeat", "privacy", "outcome", "status",
              "own_check", "independent", "facts_found", "facts", "requests", "tokens", "cost_usd", "providers", "deferrals",
              "empty_answers", "seconds", "failure")

_sleep = time.sleep


# ---------------------------------------------------------------- tasks


def norm(text: str) -> str:
    """For comparing: NFKC, case folded, no white space, no thousands commas."""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    return re.sub(r"\s+", "", text)


def numbers(text: str) -> list[float]:
    """Every number in the text, thousands commas removed. `1,450` and `1450.00` are both 1450."""
    out = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", unicodedata.normalize("NFKC", str(text))):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _contains(haystack: str, needle: str) -> bool:
    """`needle` in `haystack`, but a needle that starts or ends with a digit must not sit inside a longer number."""
    if not needle:
        return False
    pattern = ("(?<![0-9])" if needle[0].isdigit() else "") + re.escape(needle) + ("(?![0-9])" if needle[-1].isdigit() else "")
    return re.search(pattern, haystack) is not None


def fact_in(fact: Mapping[str, Any], text: str) -> bool:
    """Is this expected fact present in `text`? One of: `any` (substrings), `number`, `regex`."""
    if "any" in fact:
        squashed = norm(text)
        return any(_contains(squashed, norm(alt)) for alt in fact["any"])
    if "number" in fact:
        want = float(fact["number"])
        return any(abs(n - want) < 1e-9 for n in numbers(text))
    if "regex" in fact:
        return re.search(fact["regex"], unicodedata.normalize("NFKC", str(text)), re.IGNORECASE) is not None
    return False


def sources_of(fact: Mapping[str, Any]) -> list[str]:
    source = fact.get("source")
    return [source] if isinstance(source, str) else list(source or [])


def task_problems(task: Any, tasks_dir: Path, skill: str) -> list[str]:
    """What is wrong with one task definition; [] when it can be used."""
    if not isinstance(task, dict):
        return ["not a JSON object"]
    out: list[str] = []
    for key in ("id", "kind", "lang"):
        if not isinstance(task.get(key), str) or not task[key]:
            out.append(f"`{key}` must be a non-empty string")
    if task.get("skill") != skill:
        out.append(f"`skill` must be {skill!r}, the folder the file is in")
    if not isinstance(task.get("inputs"), dict) or not task.get("inputs"):
        out.append("`inputs` must be a non-empty object")
    folders = task.get("folders", [])
    if not isinstance(folders, list) or not all(isinstance(f, str) and f and "/" not in f and ".." not in f for f in folders):
        out.append("`folders` must be a list of folder names under fixtures/")
        folders = []
    for folder in folders:
        if not (tasks_dir / FIXTURES / folder).is_dir():
            out.append(f"fixture folder not found: {folder}")
    expect = task.get("expect")
    if not isinstance(expect, dict):
        return out + ["`expect` must be an object"]
    facts = expect.get("facts", [])
    if not isinstance(facts, list) or not all(isinstance(f, dict) for f in facts):
        return out + ["`expect.facts` must be a list of objects"]
    if expect.get("not_found") is not True and not facts:
        out.append("`expect` needs `facts`, or `not_found: true`")
    if expect.get("not_found") is True and facts:
        out.append("`expect` has both `not_found` and `facts`")
    for number, fact in enumerate(facts, start=1):
        kinds = [k for k in ("any", "number", "regex") if k in fact]
        if len(kinds) != 1:
            out.append(f"fact {number} needs exactly one of `any`, `number`, `regex`")
            continue
        if "any" in fact and not (isinstance(fact["any"], list) and fact["any"] and all(isinstance(a, str) and a for a in fact["any"])):
            out.append(f"fact {number}: `any` must be a list of strings")
        if "number" in fact and (isinstance(fact["number"], bool) or not isinstance(fact["number"], (int, float))):
            out.append(f"fact {number}: `number` must be a number")
        if "regex" in fact:
            try:
                re.compile(fact["regex"])
            except (re.error, TypeError):
                out.append(f"fact {number}: `regex` does not compile")
    return out


def fixture_problems(task: Mapping[str, Any], tasks_dir: Path) -> list[str]:
    """Expected facts that are not in the file they should come from: such a task could not be answered."""
    out: list[str] = []
    for number, fact in enumerate(task.get("expect", {}).get("facts", []), start=1):
        names = sources_of(fact)
        if not names:
            continue
        texts = []
        for name in names:
            found = [tasks_dir / FIXTURES / folder / name for folder in task.get("folders", [])]
            found = [p for p in found if p.is_file()]
            if not found:
                out.append(f"fact {number}: source file {name} is not in the task's folders")
            texts += [p.read_text(encoding="utf-8") for p in found]
        if texts and not any(fact_in(fact, text) for text in texts):
            out.append(f"fact {number} is not in {' or '.join(names)}")
    return out


def load_tasks(tasks_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """({skill folder: tasks}, problems). A skill folder is any folder with a tasks.jsonl."""
    found: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    if not tasks_dir.is_dir():
        return {}, [f"no such folder: {tasks_dir}"]
    for folder in sorted(p for p in tasks_dir.iterdir() if p.is_dir() and p.name != FIXTURES and (p / TASK_FILE).is_file()):
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for number, line in enumerate((folder / TASK_FILE).read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            where = f"{folder.name}/{TASK_FILE} line {number}"
            try:
                task = json.loads(line)
            except ValueError:
                problems.append(f"{where}: not JSON")
                continue
            issues = task_problems(task, tasks_dir, folder.name)
            if not issues and task["id"] in seen:
                issues = [f"duplicate id {task['id']}"]
            if not issues:
                issues = fixture_problems(task, tasks_dir)
            if issues:
                problems += [f"{where}: {issue}" for issue in issues]
                continue
            seen.add(task["id"])
            tasks.append(task)
        found[folder.name] = tasks
    return found, problems


def set_hash(tasks: Iterable[Mapping[str, Any]], tasks_dir: Path) -> str:
    """Changes when a task or a fixture file changes, so two nights are only compared on the same set."""
    digest = hashlib.sha256()
    folders: set[str] = set()
    for task in sorted(tasks, key=lambda t: t["id"]):
        digest.update(json.dumps(task, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        folders.update(task.get("folders", []))
    for folder in sorted(folders):
        for path in sorted((tasks_dir / FIXTURES / folder).rglob("*")):
            if path.is_file():
                digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()[:12]


def choose(tasks: list[dict[str, Any]], sample: int | None, seed: int) -> list[dict[str, Any]]:
    """All tasks, or a random `sample` of them spread over the kinds (the same for the same seed)."""
    if not sample or sample >= len(tasks):
        return list(tasks)
    rng = random.Random(seed)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        by_kind.setdefault(task["kind"], []).append(task)
    for group in by_kind.values():
        rng.shuffle(group)
    picked: list[dict[str, Any]] = []
    while len(picked) < sample:
        for kind in sorted(by_kind):
            if by_kind[kind] and len(picked) < sample:
                picked.append(by_kind[kind].pop())
    order = {task["id"]: i for i, task in enumerate(tasks)}
    return sorted(picked, key=lambda t: order[t["id"]])


# ---------------------------------------------------------------- scoring


def answer_text(output: Any) -> str:
    if isinstance(output, dict):
        for key in ("text", "answer", "report"):
            if isinstance(output.get(key), str) and output[key].strip():
                return output[key]
        return ""
    return output if isinstance(output, str) else ""


def cited_files(output: Any) -> set[str] | None:
    """Names of the files the answer cites; None when the skill's output has no citation list at all."""
    if not isinstance(output, dict):
        return None
    lists = [output[key] for key in ("citations", "sources") if isinstance(output.get(key), list)]
    if not lists:
        return None
    return {Path(str(item["path"])).name for items in lists for item in items if isinstance(item, dict) and item.get("path")}


def score(task: Mapping[str, Any], output: Any) -> dict[str, Any]:
    """The independent score: are the expected facts in the answer, citing the right file?"""
    expect = task["expect"]
    text = answer_text(output)
    if expect.get("not_found") is True:
        said = isinstance(output, dict) and output.get("not_found") is True
        return {"pass": bool(said), "facts_found": 0, "facts": 0, "sources_ok": None, "not_found": bool(said)}
    facts = expect.get("facts", [])
    found = [fact_in(fact, text) for fact in facts]
    cited = cited_files(output)
    wanted = [sources_of(fact) for fact in facts if sources_of(fact)]
    sources_ok = None if cited is None or not wanted else all(any(name in cited for name in names) for names in wanted)
    claimed_nothing = isinstance(output, dict) and output.get("not_found") is True
    return {"pass": bool(text) and all(found) and sources_ok is not False and not claimed_nothing,
            "facts_found": sum(found), "facts": len(facts), "sources_ok": sources_ok, "not_found": claimed_nothing}


def wilson(passed: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """The 95% Wilson score interval for a pass rate; None when nothing was counted."""
    if total <= 0:
        return None
    p = passed / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


REASON_CODES = (
    ("without a source", "uncited"), ("cites no", "uncited"), ("no cited claims", "empty"), ("no statement", "empty"),
    ("is empty", "empty"), ("does not exist", "unknown-source"), ("was not given", "unknown-source"),
    ("shares too little", "weak-overlap"), ("nothing that can be checked", "weak-overlap"), ("too long", "length"),
    ("language", "language"), ("sub-question", "coverage"), ("sources list", "source-list"),
    ("not found but", "not-found-with-claims"), ("nothing was found but", "not-found-with-claims"),
    ("check itself stopped", "check-error"),
)


def reason_code(reason: str) -> str:
    """A check reason as a short code. The wording stays out of the report."""
    low = str(reason).lower()
    return next((code for needle, code in REASON_CODES if needle in low), "other")


def ended_by_deferral(events: list[dict[str, Any]]) -> bool:
    """Was the last thing the router did in this run a deferral that nothing came after?"""
    for event in reversed(events):
        if event.get("kind") == "deferred":
            return True
        if event.get("kind") in ("call", "wait"):
            return False
    return False


def record_of(task: Mapping[str, Any], repeat: int, result: Any, seconds: float, keep: bool) -> dict[str, Any]:
    """One run as plain facts: no document text, no prompt, no answer (unless `keep`)."""
    events = list(result.events or [])
    used = result.used or {}
    calls = [e for e in events if e.get("kind") == "call"]
    deferrals = [e for e in events if e.get("kind") == "deferred"]
    own = (result.check or {}).get("status", "not-run")
    reasons = [reason_code(r) for r in (result.check or {}).get("reasons", [])]
    unreachable = any(e.get("status") == 0 and not e.get("ok") for e in calls)
    failure = ""
    if result.status == "needs_choice" and ended_by_deferral(events):
        outcome = "deferred"
    elif result.status == "failed" and unreachable:
        outcome = "no-router"
    elif result.status in ("done", "partial") and own == "passed":
        outcome = "passed"
    else:
        outcome = "failed"
        if own == "failed":
            failure = "check-failed"
        elif result.refusal:
            failure = "privacy-refused"
        elif any(e.get("kind") == "error" for e in events):
            failure = "skill-error"
        elif any("budget of" in note for note in result.notes or []):
            failure = "budget"
        elif any(not e.get("ok") for e in calls):
            failure = "router-error"
        elif not events:
            failure = "not-ready"
        else:
            failure = "stopped"
    scored = score(task, result.output) if outcome in ("passed", "failed") else None
    record: dict[str, Any] = {
        "skill": task["skill"], "task": task["id"], "kind": task["kind"], "lang": task["lang"], "repeat": repeat,
        "outcome": outcome, "status": result.status, "own_check": own, "partial": result.status == "partial",
        "failure": failure, "check_reasons": {code: reasons.count(code) for code in sorted(set(reasons))},
        "independent": scored,
        "requests": int(used.get("requests", 0)), "tokens": int(used.get("tokens", 0)), "cost_usd": float(used.get("cost_usd", 0.0)),
        "providers": {name: int(row.get("requests", 0)) for name, row in (used.get("providers") or {}).items()},
        "deferrals": len(deferrals),
        "offered": ["+".join(o.get("choice", "?") for o in e.get("options") or []) or "none" for e in deferrals],
        "options": [o.get("choice", "?") for o in result.options or []] if outcome == "deferred" else [],
        "waits": sum(1 for e in events if e.get("kind") == "wait"),
        "empty_answers": sum(1 for e in calls if e.get("answer") == "empty-reasoning"),
        "seconds": round(seconds, 2),
    }
    if keep:
        record["kept"] = {"text": answer_text(result.output), "notes": list(result.notes or []),
                          "reasons": list((result.check or {}).get("reasons", []))}
    return record


# ---------------------------------------------------------------- summary


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def _interval(passed: int, total: int) -> list[float] | None:
    got = wilson(passed, total)
    return [round(got[0], 4), round(got[1], 4)] if got else None


def summarise(records: list[dict[str, Any]], planned: int) -> dict[str, Any]:
    completed = [r for r in records if r["outcome"] in ("passed", "failed")]
    deferred = [r for r in records if r["outcome"] == "deferred"]
    own = [r for r in completed if r["outcome"] == "passed"]
    right = [r for r in completed if r["independent"] and r["independent"]["pass"]]
    both = [r for r in own if r["independent"] and r["independent"]["pass"]]
    failures: dict[str, int] = {}
    reasons: dict[str, int] = {}
    providers: dict[str, int] = {}
    offered: dict[str, int] = {}
    for r in records:
        if r["failure"]:
            failures[r["failure"]] = failures.get(r["failure"], 0) + 1
        for code, n in r["check_reasons"].items():
            if r["outcome"] == "failed":
                reasons[code] = reasons.get(code, 0) + n
        for name, n in r["providers"].items():
            providers[name] = providers.get(name, 0) + n
        for item in r["offered"]:
            offered[item] = offered.get(item, 0) + 1

    def group(key: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in completed:
            row = out.setdefault(r[key], {"completed": 0, "passed_own": 0, "passed_independent": 0})
            row["completed"] += 1
            row["passed_own"] += r["outcome"] == "passed"
            row["passed_independent"] += bool(r["independent"] and r["independent"]["pass"])
        return dict(sorted(out.items()))

    cost = round(sum(r["cost_usd"] for r in records), 6)
    n = len(completed)
    met = n > 0 and len(own) / n >= G1_RATE and cost == 0
    return {
        "planned": planned, "runs": len(records), "completed": n, "deferred": len(deferred),
        "no_router": sum(1 for r in records if r["outcome"] == "no-router"), "not_run": max(0, planned - len(records)),
        "passed_own": len(own), "passed_independent": len(right), "passed_both": len(both),
        "own_passed_but_wrong": len(own) - len(both), "partial": sum(1 for r in own if r["partial"]),
        "failed": n - len(own), "failures": dict(sorted(failures.items())), "check_reasons": dict(sorted(reasons.items())),
        "rate_own": _rate(len(own), n), "interval_own": _interval(len(own), n),
        "rate_independent": _rate(len(right), n), "interval_independent": _interval(len(right), n),
        "requests": sum(r["requests"] for r in records), "tokens": sum(r["tokens"] for r in records), "cost_usd": cost,
        "providers": dict(sorted(providers.items())), "deferrals": sum(r["deferrals"] for r in records),
        "offered": dict(sorted(offered.items())), "empty_answers": sum(r["empty_answers"] for r in records),
        "seconds": round(sum(r["seconds"] for r in records), 1), "by_kind": group("kind"), "by_lang": group("lang"),
        "g1": {"met": met, "completed": n, "enough_runs": n >= G1_RUNS, "nothing_spent": cost == 0},
    }


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _span(interval: list[float] | None) -> str:
    return "" if not interval else f"  (95% interval {interval[0] * 100:.1f}% to {interval[1] * 100:.1f}%)"


def g1_line(summary: Mapping[str, Any]) -> str:
    g1 = summary["g1"]
    line = (f"G1: at least {G1_RATE * 100:.0f}% of completed runs passed their own check with nothing spent: "
            f"{'yes' if g1['met'] else 'no'} ({summary['passed_own']} of {g1['completed']}")
    line += ")" if g1["nothing_spent"] else f"; US${summary['cost_usd']:.6f} WAS SPENT)"
    if not g1["enough_runs"]:
        line += f" - only {g1['completed']} completed runs; the gate asks for {G1_RUNS} per skill"
    return line


def render(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for name, entry in report["skills"].items():
        s = entry.get("summary")
        if not s:
            continue
        n = s["completed"]
        lines.append(f"{name} {entry.get('version', '')}: {s['planned']} planned, {n} completed, {s['deferred']} deferred, "
                     f"{s['not_run'] + s['no_router']} not run")
        lines.append(f"  passed own check         {s['passed_own']:>4} of {n}  {_pct(s['rate_own'])}{_span(s['interval_own'])}")
        lines.append(f"  right by expected facts  {s['passed_independent']:>4} of {n}  {_pct(s['rate_independent'])}{_span(s['interval_independent'])}")
        lines.append(f"  both                     {s['passed_both']:>4} of {n}")
        if s["partial"]:
            lines.append(f"  of the passes, {s['partial']} were partial (something could not be done; the notes said what)")
        if s["failed"]:
            why = ", ".join(f"{k} {v}" for k, v in s["failures"].items())
            detail = ", ".join(f"{k} {v}" for k, v in s["check_reasons"].items())
            lines.append(f"  failed                   {s['failed']:>4}: {why}" + (f"  [check reasons: {detail}]" if detail else ""))
        if s["deferred"] or s["deferrals"]:
            offered = ", ".join(f"{k} x{v}" for k, v in s["offered"].items()) or "nothing"
            lines.append(f"  deferred                 {s['deferred']:>4}  (reported apart: neither passes nor failures; --resume tries them again)"
                         f"  [{s['deferrals']} deferrals in all; offered: {offered}]")
        answered = ", ".join(f"{k} {v}" for k, v in s["providers"].items()) or "nobody"
        lines.append(f"  used {s['requests']} requests, {s['tokens']} tokens, US${s['cost_usd']:.6f}, {s['seconds']:.0f} s; answered by: {answered}"
                     + (f"; {s['empty_answers']} empty answers (reasoning used the whole output)" if s["empty_answers"] else ""))
        if s["by_kind"]:
            lines.append("  own check by kind: " + ", ".join(f"{k} {v['passed_own']}/{v['completed']}" for k, v in s["by_kind"].items()))
            lines.append("  own check by language: " + ", ".join(f"{k} {v['passed_own']}/{v['completed']}" for k, v in s["by_lang"].items()))
        if entry.get("stopped"):
            lines.append(f"  stopped early: {entry['stopped']}")
        lines.append("  " + g1_line(s))
        if s["own_passed_but_wrong"]:
            much = s["rate_own"] is not None and s["rate_independent"] is not None and s["rate_own"] - s["rate_independent"] >= 0.10
            lines.append(f"  {'CAVEAT' if much else 'Note'}: the skill's own check passed answers that were wrong "
                         f"{s['own_passed_but_wrong']} time(s) by the expected facts"
                         + ("; the own-check rate overstates how often it is right." if much else "."))
        lines.append("")
    for item in report.get("skipped", []):
        lines.append(f"{item['skill']}: skipped - {item['reason']}")
    overall = report.get("verdict", {})
    if overall:
        lines.append("")
        lines.append(f"Overall G1 line (every skill that ran): {'yes' if overall['met'] else 'no'}"
                     + ("" if overall["enough_runs"] else f" - not every skill has {G1_RUNS} completed runs yet")
                     + f". Spent: US${overall['cost_usd']:.6f}.")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- files


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_csv(path: Path, report: Mapping[str, Any], record: Mapping[str, Any], version: str, set_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    scored = record.get("independent") or {}
    row = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": report["run_id"], "set": set_id,
        "skill": record["skill"], "version": version, "task": record["task"], "kind": record["kind"], "lang": record["lang"],
        "repeat": record["repeat"], "privacy": report["privacy"], "outcome": record["outcome"], "status": record["status"],
        "own_check": record["own_check"], "independent": "" if not scored else ("pass" if scored["pass"] else "fail"),
        "facts_found": scored.get("facts_found", ""), "facts": scored.get("facts", ""), "requests": record["requests"],
        "tokens": record["tokens"], "cost_usd": f"{record['cost_usd']:.6f}",
        "providers": "+".join(sorted(record["providers"])), "deferrals": record["deferrals"],
        "empty_answers": record["empty_answers"], "seconds": record["seconds"], "failure": record["failure"],
    }
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def merged_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Environment, .env and key store merged the way the other real-provider scripts do it."""
    try:
        from muse import keys

        return dict(keys.export_env(environ))
    except Exception:
        return dict(os.environ if environ is None else environ)


# ---------------------------------------------------------------- the run


def key_of(task: Mapping[str, Any], repeat: int) -> str:
    return f"{task['skill']}/{task['id']}#{repeat}"


def run(args: argparse.Namespace, env: dict[str, str]) -> int:
    from muse import __version__, paths
    from muse.skills import loader, runner

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else TASKS_DIR
    found, problems = load_tasks(tasks_dir)
    if problems:
        print("The task files have problems; nothing was sent:", file=sys.stderr)
        for problem in problems[:40]:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_USAGE
    if args.check_tasks:
        for name, tasks in found.items():
            kinds: dict[str, int] = {}
            for task in tasks:
                kinds[task["kind"]] = kinds.get(task["kind"], 0) + 1
            print(f"{name}: {len(tasks)} tasks ok (" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")")
        return EXIT_OK
    only = {name.strip() for name in (args.only or "").split(",") if name.strip()}
    unknown = only - set(found)
    if unknown:
        print(f"No task file for: {', '.join(sorted(unknown))} (have: {', '.join(sorted(found)) or 'none'})", file=sys.stderr)
        return EXIT_USAGE

    report_path = Path(args.report) if args.report else paths.home(env) / "chores-report.json"
    csv_path = Path(args.csv) if args.csv else paths.home(env) / "chores-runs.csv"
    previous: dict[str, Any] = {}
    if args.resume and report_path.is_file():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8")).get("runs", {})
        except (ValueError, AttributeError):
            previous = {}
    kept = {key: record for key, record in previous.items() if record.get("outcome") in ("passed", "failed")}

    report: dict[str, Any] = {
        "version": 1, "tool": "chores_check", "muse_version": __version__,
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "finished": None,
        "privacy": args.privacy, "paid_usd": 0, "app": "skills",
        "options": {"runs": args.runs, "sample": args.sample, "seed": args.seed, "only": sorted(only), "delay": args.delay,
                    "max_wait": args.max_wait, "resume": bool(args.resume), "keep_outputs": bool(args.keep_outputs)},
        "skills": {}, "skipped": [], "runs": dict(kept), "stopped": None, "verdict": {},
    }

    # ---- which skills can run, and the plan
    work: dict[str, list[tuple[dict[str, Any], int]]] = {}
    planned: dict[str, int] = {}
    for name, tasks in found.items():
        if only and name not in only:
            continue
        try:
            skill = loader.find(name, env)
            loader.require_ready(skill)
        except loader.LoaderError as exc:
            why = "this skill is not installed here" if "no skill named" in str(exc) else "this skill is not ready to run here"
            report["skipped"].append({"skill": name, "reason": f"{why}, so its {len(tasks)} tasks were not run"})
            continue
        if not tasks:
            report["skipped"].append({"skill": name, "reason": "its task file is empty"})
            continue
        chosen = choose(tasks, args.sample, args.seed)
        every = [(task, repeat) for task in chosen for repeat in range(1, args.runs + 1)]
        work[name] = [(task, repeat) for task, repeat in every if key_of(task, repeat) not in kept]
        planned[name] = len(every)
        report["skills"][name] = {
            "version": skill.version, "tasks": len(chosen), "set": set_hash(chosen, tasks_dir),
            "budget": {"requests": skill.budget.requests, "tokens": skill.budget.tokens, "seconds": skill.budget.seconds}}

    print("Muse chores check: what this will send")
    print(f"  router: {runner.proxy_url(env)}   app: skills (never paid)   privacy: {args.privacy}")
    total_runs = total_requests = total_tokens = 0
    for name, todo in work.items():
        budget = report["skills"][name]["budget"]
        requests, tokens = len(todo) * budget["requests"], len(todo) * budget["tokens"]
        total_runs, total_requests, total_tokens = total_runs + len(todo), total_requests + requests, total_tokens + tokens
        done = planned[name] - len(todo)
        print(f"  {name:<20} {len(todo):>4} runs ({report['skills'][name]['tasks']} tasks x {args.runs})"
              + (f", {done} already done" if done else "")
              + f": at most {requests} requests and {tokens} tokens ({budget['requests']} and {budget['tokens']} a run, the skill's own budget)")
    for item in report["skipped"]:
        print(f"  {item['skill']:<20} skipped: {item['reason']}")
    print(f"  worst case in all: {total_runs} runs, {total_requests} requests, {total_tokens} tokens. These are ceilings (each skill's own budget), not estimates.")
    print(f"  One run at a time, {args.delay:g} s apart. A Retry-After up to {args.max_wait:g} s is waited for; a longer one records the run as deferred.")
    print("  Paid providers are not used; the run stops if anything is charged.")
    print(f"  report: {report_path}" + ("  (WITH answers and check reasons: they hold text from the test documents)" if args.keep_outputs
                                         else "  (ids, counts and codes; no document text, prompts or answers)"))
    print(f"  one line per run is appended to: {csv_path}")
    if total_runs == 0:
        print("Nothing to run.")
        return finish(report, report_path, planned, found, {})
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not a terminal: pass --yes to confirm.", file=sys.stderr)
            return EXIT_USAGE
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was sent.")
            return EXIT_OK
    print()

    # ---- the runs: one at a time
    first = True
    for name, todo in work.items():
        entry = report["skills"][name]
        deferred_streak = unreachable_streak = 0
        for index, (task, repeat) in enumerate(todo, start=1):
            if not first and args.delay > 0:
                _sleep(args.delay)
            first = False
            folders = [tasks_dir / FIXTURES / folder for folder in task.get("folders", [])]
            began = time.monotonic()
            result = runner.run(name, task["inputs"], folders=folders, privacy=args.privacy, budget={"paid_usd": 0},
                                environ=env, wait_up_to=args.max_wait)
            record = record_of(task, repeat, result, time.monotonic() - began, args.keep_outputs)
            report["runs"][key_of(task, repeat)] = record
            append_csv(csv_path, report, record, entry["version"], entry["set"])
            if not args.quiet:
                scored = record["independent"]
                facts = "" if not scored else f", facts {scored['facts_found']}/{scored['facts']}" + ("" if scored["pass"] else " WRONG")
                print(f"  {name:<18} {index:>3}/{len(todo)}  {task['id']:<10} {record['outcome']:<9}"
                      f" own check {record['own_check']}{facts}, {record['requests']} requests"
                      + (f", {record['failure']}" if record["failure"] else ""))
            if record["cost_usd"] > 0:
                report["stopped"] = "spend"
                write_report(report, report_path)
                print("\n" + "!" * 78, file=sys.stderr)
                print(f"!! STOPPED: the router reported a cost of US${record['cost_usd']:.6f} for {name} {task['id']}.", file=sys.stderr)
                print("!! This check must cost nothing. No further run was started. Look at `muse status` and the", file=sys.stderr)
                print("!! `skills` app (it must not be allowed to pay) before running this again.", file=sys.stderr)
                print("!" * 78, file=sys.stderr)
                finish(report, report_path, planned, found, work)
                return EXIT_SPENT
            deferred_streak = deferred_streak + 1 if record["outcome"] == "deferred" else 0
            unreachable_streak = unreachable_streak + 1 if record["outcome"] == "no-router" else 0
            if unreachable_streak >= UNREACHABLE_STOP:
                report["stopped"] = "no-router"
                print("\nThe router could not be reached. Start it with `muse up`, then run this again with --resume.", file=sys.stderr)
                finish(report, report_path, planned, found, work)
                return EXIT_NOT_MET
            if deferred_streak >= args.stop_after:
                entry["stopped"] = f"{deferred_streak} runs in a row were deferred; the free allowance for this skill is used up for now"
                print(f"  {name}: stopped after {deferred_streak} deferred runs in a row; --resume continues it later.")
                break
            if index % 5 == 0:
                write_report(report, report_path)
    return finish(report, report_path, planned, found, work)


def finish(report: dict[str, Any], path: Path, planned: Mapping[str, int], found: Mapping[str, list[dict[str, Any]]],
           work: Mapping[str, Any]) -> int:
    for name, entry in report["skills"].items():
        records = [r for r in report["runs"].values() if r["skill"] == name]
        entry["summary"] = summarise(records, planned.get(name, len(records)))
    summaries = [e["summary"] for e in report["skills"].values()]
    report["verdict"] = {
        "met": bool(summaries) and all(s["g1"]["met"] for s in summaries),
        "enough_runs": bool(summaries) and all(s["g1"]["enough_runs"] for s in summaries),
        "cost_usd": round(sum(s["cost_usd"] for s in summaries), 6),
        "per_skill": {name: e["summary"]["g1"] for name, e in report["skills"].items()},
    }
    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_report(report, path)
    print()
    print(render(report), end="")
    print(f"Report: {path}")
    if report["stopped"] == "spend":
        return EXIT_SPENT
    return EXIT_OK if report["verdict"]["met"] else EXIT_NOT_MET


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run each shipped skill many times on free allowances and report how often it passes (gate G1).")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--only", metavar="SKILLS", help="comma-separated skill names (default: every skill with a task file)")
    parser.add_argument("--runs", type=int, default=1, metavar="N", help="times each task is run (default 1)")
    parser.add_argument("--sample", type=int, metavar="K", help="a random K tasks per skill, spread over the kinds")
    parser.add_argument("--seed", type=int, default=0, help="seed for --sample (default 0)")
    parser.add_argument("--resume", action="store_true", help="continue the report at --report: runs that passed or failed are kept, the rest are run")
    parser.add_argument("--privacy", default="standard", choices=("public", "standard", "private", "local"),
                        help="the privacy class of every run (default standard; the test documents are fictional)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"seconds between runs (default {DEFAULT_DELAY:g})")
    parser.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT,
                        help=f"longest Retry-After to sit out, in seconds; a longer one records the run as deferred (default {DEFAULT_MAX_WAIT:g})")
    parser.add_argument("--stop-after", type=int, default=DEFAULT_STOP_AFTER,
                        help=f"stop a skill after this many deferred runs in a row (default {DEFAULT_STOP_AFTER})")
    parser.add_argument("--report", metavar="PATH", help="default: $MUSE_HOME/chores-report.json")
    parser.add_argument("--csv", metavar="PATH", help="default: $MUSE_HOME/chores-runs.csv (one line per run, appended)")
    parser.add_argument("--keep-outputs", action="store_true", help="store answers, notes and check reasons in the report (default: none of them)")
    parser.add_argument("--tasks-dir", metavar="DIR", help="read task files and fixtures from here instead of evals/chores")
    parser.add_argument("--check-tasks", action="store_true", help="only validate the task files against the fixtures; sends nothing")
    parser.add_argument("--quiet", action="store_true", help="no line per run")
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    if args.runs < 1 or (args.sample is not None and args.sample < 1) or args.delay < 0 or args.max_wait < 0 or args.stop_after < 1:
        print("--runs, --sample and --stop-after must be 1 or more; --delay and --max-wait 0 or more.", file=sys.stderr)
        return EXIT_USAGE
    return run(args, merged_env(environ))


if __name__ == "__main__":
    raise SystemExit(main())
