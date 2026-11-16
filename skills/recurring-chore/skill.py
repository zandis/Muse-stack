"""Recurring chores: understand one sentence, or do one run of a chore.

Entry: `run(ctx)`. Check: `check(output, ctx)`.

  mode=understand   the sentence is read by RULES (muse/skills/schedule.py). Only
                    when the rules cannot tell how often or which chore, ONE
                    `light` call asks for a small JSON object; it is read
                    defensively and passed through the same rules as a proposal.
                    It can never supply a folder, an address or a destination.
  mode=run          one of three templates (templates.py), ending in one
                    proposed dated Markdown file. Nothing is written here.

The check for a run: every source it was given has at least one point or is
listed under "could not read"; points are short and in the right language; no
more than the budgeted excerpt of any source was sent; the dated file name is
new. A failed check is retried once by the runner with every step one tier
stronger.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
import re
from typing import Any

from wardmuse.skills import schedule

from . import sources, templates

PROPOSAL_TOKENS = 200


# ---------------------------------------------------------------- understand


def _proposal_prompt(sentence: str) -> str:
    return (
        "Someone described a recurring chore in one sentence. Fill in this JSON object from the sentence and reply with the "
        "JSON only.\n\n"
        '{"frequency": "daily" | "weekdays" | "weekly" | "monthly" | "spare" | null,\n'
        ' "days": [names of weekdays, for weekly],\n'
        ' "day_of_month": a number from 1 to 31 or "last", for monthly,\n'
        ' "time": "HH:MM" in 24-hour time, or null,\n'
        ' "template": "summarise-feeds" | "summarise-folder-changes" | "daily-brief" | null}\n\n'
        "\"spare\" means: whenever there is allowance to spare. summarise-feeds: digest news or feed files. "
        "summarise-folder-changes: say what changed in a folder. daily-brief: report what the helper did the day before. "
        "Use null for anything the sentence does not say. Do not guess.\n\n"
        f"SENTENCE:\n{sentence}"
    )


def read_json(text: str) -> Any:
    """The first JSON object in a model's reply: code fences and surrounding prose are ignored. None when there is none."""
    text = re.sub(r"```(?:json)?", "", str(text))
    start = text.find("{")
    while start != -1:
        depth, quoted, escaped = 0, False, False
        for index in range(start, len(text)):
            ch = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
            elif ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def run_understand(ctx: Any) -> dict[str, Any]:
    sentence = " ".join(str(ctx.inputs.get("sentence", "") or "").split())
    if not sentence:
        ctx.stop("Give the chore as a sentence: --input sentence=\"Every Monday 08:00, ...\"")
    if len(sentence) > schedule.MAX_SENTENCE:
        ctx.stop(f"The sentence is too long (over {schedule.MAX_SENTENCE} characters).")
    parsed = schedule.parse_sentence(sentence)
    proposal = None
    asked = False
    if (schedule.wants_proposal(parsed) and str(ctx.inputs.get("model", "auto")).strip().lower() != "off"
            and ctx.llm.remaining()["requests"] >= 1):
        asked = True
        try:
            reply = ctx.llm.ask([{"role": "user", "content": _proposal_prompt(sentence)}], task="light", max_tokens=PROPOSAL_TOKENS)
            proposal = read_json(reply.text)
        except ctx.llm.Error as exc:  # held back, refused, over budget: the questions stand
            ctx.event("proposal_skipped", reason=type(exc).__name__)
    out_dir = str(ctx.inputs.get("out") or "").strip() or None
    understood = schedule.understand(sentence, folders=ctx.workspace.roots(), out=out_dir, proposal=proposal,
                                     tz_name=envget(os.environ, "MUSE_TZ", "").strip())
    ctx.event("understood", ok=understood["ok"], questions=len(understood["questions"]), by=understood["parsed_by"], asked=asked)
    return {"mode": "understand", "understood": understood, "asked_model": asked, "text": schedule.understood_text(understood)}


def check_understand(output: dict[str, Any], ctx: Any) -> list[str]:
    u = output.get("understood")
    if not isinstance(u, dict):
        return ["nothing was understood"]
    reasons: list[str] = []
    if bool(u.get("ok")) != (u.get("spec") is not None) or bool(u.get("ok")) == bool(u.get("questions")):
        reasons.append("it is not clear whether the sentence was understood or a question is open")
    spec = u.get("spec")
    if spec is None:
        return reasons
    reasons.extend(schedule.spec_problems(spec))
    stated = schedule.parse_sentence(str(u.get("sentence") or ""))["fields"]  # what the rules read: a model may not overrule it
    for key in ("freq", "time", "template"):
        if key in stated and spec.get(key) != stated[key]:
            reasons.append(f"the {key} differs from what the sentence states")
    allowed = set(stated.get("sources") or ()) | set(ctx.workspace.roots() if ctx is not None else ())
    if any(os.path.realpath(src) not in {os.path.realpath(a) for a in allowed} for src in spec.get("sources") or []):
        reasons.append("a folder appears that the sentence did not name and the person did not pick")
    if set(spec.get("urls") or []) - set(stated.get("urls") or ()):
        reasons.append("a web address appears that the sentence did not name")
    return reasons


# ---------------------------------------------------------------- run


def run(ctx: Any) -> dict[str, Any]:
    mode = str(ctx.inputs.get("mode", "run") or "run").strip().lower()
    if mode == "understand":
        return run_understand(ctx)
    if mode != "run":
        ctx.stop("mode must be understand or run.")
    template = str(ctx.inputs.get("template", "") or "").strip().lower()
    if template not in templates.RUNNERS:
        ctx.stop("template must be one of: " + ", ".join(templates.RUNNERS) + ".")
    lang = str(ctx.inputs.get("lang", "en") or "en").strip()
    lang = "zh-TW" if lang.lower().replace("_", "-").startswith("zh") else "en"
    return templates.RUNNERS[template](ctx, lang)


def check(output: Any, ctx: Any = None) -> list[str]:
    """Reasons the result may not be delivered as checked; empty when it passes. Reasons carry counts, never names or text."""
    if not isinstance(output, dict):
        return ["the skill returned nothing"]
    if output.get("mode") == "understand":
        return check_understand(output, ctx)
    template = output.get("template")
    if template not in templates.LIMITS:
        return ["the result names no template"]
    limits = templates.LIMITS[template]
    lang = output.get("lang")
    srcs = output.get("sources") or []
    missed = output.get("could_not_read") or []
    markdown = str(output.get("markdown") or "")
    reasons: list[str] = []

    # 1. every source it was given is covered: a point, or listed as not read
    given = list(output.get("given") or [])
    if ctx is not None and template == "summarise-feeds":
        on_disk = [name for name, _ in sources.feed_files(ctx)[0]]
        if set(on_disk) - set(given):
            reasons.append(f"{len(set(on_disk) - set(given))} feed file(s) in the folder are missing from the digest")
    covered = {src.get("name") for src in srcs if src.get("points")}
    listed = {item.get("name") for item in missed}
    lost = [name for name in given if name not in covered and name not in listed]
    if lost:
        reasons.append(f"the digest missed {len(lost)} source(s)")
    if markdown:
        lines = set(markdown.splitlines())
        unseen = [n for n in covered if f"## {n}" not in lines and f"### {n}" not in lines]
        unseen += [item["name"] for item in missed if f"- {item['name']}: {item.get('text', '')}" not in lines]
        unseen += [n for n in output.get("deleted") or [] if f"- {n}" not in lines]
        if unseen:
            reasons.append(f"{len(unseen)} source(s) are not shown in the file")
    elif given or output.get("deleted"):
        reasons.append("there were sources but no file was made")

    # 2. length bounds
    long = sum(1 for src in srcs for p in src.get("points") or [] if not str(p).strip() or len(str(p)) > templates.MAX_POINT_CHARS)
    many = sum(1 for src in srcs if len(src.get("points") or []) > limits["points"])
    if long or many:
        reasons.append(f"{long} point(s) are empty or too long and {many} source(s) have too many points")
    if len(markdown) > templates.MAX_DOC_CHARS or len(str(output.get("overview") or "")) > templates.OVERVIEW_MAX_CHARS:
        reasons.append("the file is longer than a digest may be")

    # 3. the right language (the model's words only: the brief's own lines are Muse's)
    words = " ".join([str(output.get("overview") or "")] + ([] if template == "daily-brief" else
                                                              [str(p) for src in srcs for p in src.get("points") or []]))
    share, letters = templates.cjk_share(words)
    if letters >= 20 and ((lang == "zh-TW" and share < 0.2) or (lang == "en" and share > 0.5)):
        reasons.append("the digest is not in the language that was asked for")

    # 4. no more of a source was sent than the budgeted excerpt
    over = sum(1 for src in srcs if not isinstance(src.get("sent_chars"), int) or src["sent_chars"] > limits["excerpt"])
    if over or len(srcs) > limits["sources"]:
        reasons.append(f"{over} source(s) had more than the budgeted excerpt sent, or too many sources were summarised")
    if template == "daily-brief" and output.get("overview"):
        counted = " ".join(str(p) for src in srcs for p in src.get("points") or [])
        if templates.digit_runs(str(output["overview"])) - templates.digit_runs(counted):
            reasons.append("the brief's summary has a number that is not in the counts")

    # 5. the dated file name is new, and what is proposed is what was checked
    if markdown:
        name = str(output.get("filename") or "")
        match = templates.FILENAME.fullmatch(name)
        if not match or match.group(1) != output.get("date") or not name.startswith(templates.SLUGS[template]):
            reasons.append("the file name is not the dated name for this run")
        elif ctx is not None:
            if ctx.workspace.resolve_write(name).exists():
                reasons.append("a file with that dated name already exists")
            proposed = [w for w in ctx.workspace.proposed if w.path == output.get("write")]
            if len(ctx.workspace.proposed) != 1 or not proposed or proposed[0].data != markdown.encode("utf-8"):
                reasons.append("the proposed file is not the digest that was checked")
    elif ctx is not None and ctx.workspace.proposed:
        reasons.append("a file was proposed without a digest")
    return reasons
