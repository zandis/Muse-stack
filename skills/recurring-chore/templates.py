"""The chores themselves: three small pipelines, each ending in one proposed dated Markdown file.

  summarise-feeds            one `light` call per feed file on a short excerpt, then one `everyday` call for an overview
  summarise-folder-changes   new / changed / deleted since the last delivered run; one `light` call per new or changed file
  daily-brief                counts from this home's own records (no model needed); one `light` call for two sentences

Every pipeline stops cleanly when the job's budget runs out: what was done is
kept, the rest is listed under "could not read", and the result is partial.
Nothing from a file goes into a note or an event: names, counts and codes only
reach the output, which is the person's own digest.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from muse.skills import schedule

from . import fetch, sources

MAX_POINT_CHARS = 300
MAX_DOC_CHARS = 40_000
OVERVIEW_MAX_CHARS = 800
POINT_TOKENS = 260
OVERVIEW_TOKENS = 220
MAX_URLS = 50

# excerpt: the most characters of one source ever sent to a model; sources: how many get a call; points: kept per source.
LIMITS: dict[str, dict[str, int]] = {
    "summarise-feeds": {"excerpt": 6000, "sources": 30, "points": 5},
    "summarise-folder-changes": {"excerpt": 3000, "sources": 12, "points": 3},
    "daily-brief": {"excerpt": 2000, "sources": 8, "points": 8},
}
SLUGS = {"summarise-feeds": "feed-digest", "summarise-folder-changes": "folder-changes", "daily-brief": "daily-brief"}
FILENAME = re.compile(r"^[a-z-]+-(\d{4}-\d{2}-\d{2})(?:-\d{1,2})?\.md$")

S: dict[str, dict[str, str]] = {
    "en": {
        "language": "English",
        "title_feeds": "Feed digest",
        "title_changes": "What changed",
        "title_brief": "Daily brief",
        "label_feeds": "Feed digest",
        "label_changes": "Folder changes",
        "label_brief": "Daily brief",
        "overview": "Overview",
        "not_read": "Could not read",
        "new": "New",
        "changed": "Changed",
        "deleted": "Deleted",
        "counts": "{new} new, {changed} changed, {deleted} deleted since {since}.",
        "counts_first": "since the first run",
        "brief_for": "What Muse did on {day}.",
        "footer": "Made by Muse on {date}. Sources read: {read}. Not read: {missed}.",
        "r_sandbox": "a web address; fetching needs Muse's sandbox, which is not built yet",
        "r_addresses": "it only lists web addresses; fetching needs Muse's sandbox, which is not built yet",
        "r_large": "the file is too large",
        "r_unreadable": "the file could not be opened",
        "r_not-text": "not a text file",
        "r_empty": "nothing readable in it",
        "r_limit": "over the number of sources one run summarises",
        "r_budget": "this run's budget was used up first",
        "r_unavailable": "Muse could not read that part of its own records",
        "n_budget": "Stopped early: this run's budget was used up. {n} source(s) were not summarised and are listed as not read.",
        "n_not_read": "{n} source(s) could not be read; the digest lists them.",
        "n_limit": "{n} source(s) were over the number one run summarises and are listed as not read.",
        "n_ignored": "{n} file(s) of other kinds in the folder were left alone.",
        "n_no_feeds": "The folder holds no feed files, so there is nothing to summarise and no file was proposed.",
        "n_first": "First run: the folder's {n} file(s) were recorded as the starting point. Changes are reported from the next run.",
        "n_no_changes": "Nothing changed since the last run, so no file was proposed.",
        "n_no_overview": "The overview was left out: it could not be written within the rules.",
        "n_sandbox": "{n} web address(es) were not fetched: that needs Muse's sandbox, which is not built yet.",
        "n_links": "{n} link(s) pointing outside the folders were not followed.",
    },
    "zh-TW": {
        "language": "Traditional Chinese (Taiwan)",
        "title_feeds": "訂閱摘要",
        "title_changes": "變動摘要",
        "title_brief": "每日簡報",
        "label_feeds": "訂閱摘要",
        "label_changes": "資料夾變動",
        "label_brief": "每日簡報",
        "overview": "總覽",
        "not_read": "未能讀取",
        "new": "新增",
        "changed": "有變動",
        "deleted": "已刪除",
        "counts": "自 {since} 以來：新增 {new} 個、變動 {changed} 個、刪除 {deleted} 個。",
        "counts_first": "第一次執行",
        "brief_for": "Muse 在 {day} 做了什麼。",
        "footer": "由 Muse 於 {date} 產生。已讀取的來源：{read} 個。未讀取：{missed} 個。",
        "r_sandbox": "這是網址；抓取網址需要 Muse 尚未完成的沙盒",
        "r_addresses": "裡面只有網址；抓取網址需要 Muse 尚未完成的沙盒",
        "r_large": "檔案太大",
        "r_unreadable": "檔案無法開啟",
        "r_not-text": "不是文字檔",
        "r_empty": "裡面沒有可讀的內容",
        "r_limit": "超過一次執行能摘要的來源數量",
        "r_budget": "這次執行的預算先用完了",
        "r_unavailable": "Muse 讀不到自己這部分的紀錄",
        "n_budget": "提早停止：這次執行的預算用完了。有 {n} 個來源沒有摘要，已列為未讀取。",
        "n_not_read": "有 {n} 個來源無法讀取，摘要裡已列出。",
        "n_limit": "有 {n} 個來源超過一次執行能摘要的數量，已列為未讀取。",
        "n_ignored": "資料夾裡另有 {n} 個其他類型的檔案，沒有處理。",
        "n_no_feeds": "資料夾裡沒有訂閱檔案，所以沒有東西可以摘要，也沒有提出檔案。",
        "n_first": "第一次執行：已記錄資料夾裡的 {n} 個檔案作為起點。下次執行開始回報變動。",
        "n_no_changes": "從上次執行以來沒有變動，所以沒有提出檔案。",
        "n_no_overview": "沒有附上總覽：無法在規則內寫出來。",
        "n_sandbox": "有 {n} 個網址沒有抓取：那需要 Muse 尚未完成的沙盒。",
        "n_links": "有 {n} 個指向資料夾外面的連結沒有跟進。",
    },
}


def s(key: str, lang: str, **fmt: Any) -> str:
    text = (S.get(lang) or S["en"]).get(key) or S["en"][key]
    return text.format(**fmt) if fmt else text


# ---------------------------------------------------------------- small pieces


def cjk_share(text: str) -> tuple[float, int]:
    """(share of Chinese characters among letters, how many letters there are)."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return (cjk / (cjk + latin) if cjk + latin else 0.0), cjk + latin


def points_of(text: str, cap: int) -> list[str]:
    """The bullet points of a model's reply, cleaned and capped. A short reply without bullets is read line by line."""
    found = [m.group(1) for line in text.splitlines() if (m := re.match(r"^\s*(?:[-*•·‧－–—]|\d{1,2}[.)、])\s*(.+)$", line))]
    if not found and len(text) <= 2 * MAX_POINT_CHARS:
        found = [line for line in text.splitlines() if line.strip()]
    out: list[str] = []
    for point in found:
        point = " ".join(point.replace("**", "").replace("`", "").split()).lstrip("#>-*• ").strip()
        if point and point not in out:
            out.append(point if len(point) <= MAX_POINT_CHARS else point[:MAX_POINT_CHARS - 1].rstrip() + "…")
    return out[:cap]


def digit_runs(text: str) -> set[str]:
    return set(re.findall(r"\d+", re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)))


def _urls(raw: Any) -> list[str]:
    try:
        items = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
    except ValueError:
        items = []
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str) and re.fullmatch(r"https?://\S{1,990}", item) and item not in out:
            out.append(item)
    return out[:MAX_URLS]


def _date(ctx: Any) -> str:
    raw = str(ctx.inputs.get("date") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return schedule.local_date(time.time(), os.environ.get("MUSE_TZ", "").strip() or None)


def _can_ask(ctx: Any, tokens: int) -> bool:
    left = ctx.llm.remaining()
    return left["requests"] >= 1 and left["tokens"] >= tokens and left["seconds"] > 0


def _ask_points(ctx: Any, instruction: str, label: str, excerpt: str, cap: int) -> list[str] | None:
    """One light call for the points of one source. None when the budget would not allow it."""
    prompt = f"{instruction}\n\n{label}:\n{excerpt}"
    if not _can_ask(ctx, len(prompt) + POINT_TOKENS):
        return None
    try:
        reply = ctx.llm.ask([{"role": "user", "content": prompt}], task="light", max_tokens=POINT_TOKENS)
    except ctx.llm.BudgetExceeded:
        return None
    return points_of(reply.text, cap)


def _filename(ctx: Any, template: str, date: str) -> str:
    """A dated name that does not exist yet in the output folder."""
    base = f"{SLUGS[template]}-{date}"
    for n in range(1, 100):
        name = f"{base}.md" if n == 1 else f"{base}-{n}.md"
        if not ctx.workspace.resolve_write(name).exists():
            return name
    return f"{base}-99.md"


def _finish(ctx: Any, out: dict[str, Any], markdown: str) -> dict[str, Any]:
    lang, template = out["lang"], out["template"]
    reasons = [item["reason"] for item in out["could_not_read"]]
    if "budget" in reasons:
        ctx.note(s("n_budget", lang, n=reasons.count("budget")), incomplete=True)
    if "limit" in reasons:
        ctx.note(s("n_limit", lang, n=reasons.count("limit")), incomplete=True)
    if "sandbox" in reasons or "addresses" in reasons:
        ctx.note(s("n_sandbox", lang, n=reasons.count("sandbox") + reasons.count("addresses")), incomplete=True)
    other = [r for r in reasons if r not in ("budget", "limit", "sandbox", "addresses")]
    if other:
        ctx.note(s("n_not_read", lang, n=len(other)), incomplete=True)
    if ctx.workspace.skipped_links:
        ctx.note(s("n_links", lang, n=ctx.workspace.skipped_links))
    out["markdown"] = markdown
    out["text"] = markdown
    out["filename"] = ""
    out["write"] = ""
    if markdown:
        out["filename"] = _filename(ctx, template, out["date"])
        write = ctx.workspace.propose_write(out["filename"], markdown, out["label"])
        out["write"] = write.path
    out["stats"] = {"given": len(out["given"]), "summarised": sum(1 for src in out["sources"] if src["points"]),
                    "not_read": len(out["could_not_read"]), "sent_chars": sum(src["sent_chars"] for src in out["sources"])}
    ctx.event("digest", given=out["stats"]["given"], summarised=out["stats"]["summarised"], not_read=out["stats"]["not_read"],
              chars=len(markdown), proposed=bool(markdown))
    return out


def _label_key(template: str) -> str:
    return {"summarise-feeds": "feeds", "summarise-folder-changes": "changes", "daily-brief": "brief"}[template]


def _base(ctx: Any, template: str, lang: str) -> dict[str, Any]:
    key = _label_key(template)
    return {"mode": "run", "template": template, "lang": lang, "date": _date(ctx), "title": s(f"title_{key}", lang),
            "label": s(f"label_{key}", lang), "given": [], "sources": [], "could_not_read": [], "overview": "",
            "limits": dict(LIMITS[template], point_chars=MAX_POINT_CHARS)}


def _missed(out: dict[str, Any], name: str, reason: str) -> None:
    out["could_not_read"].append({"name": name, "reason": reason, "text": s(f"r_{reason}", out["lang"])})


def _not_read_block(out: dict[str, Any]) -> list[str]:
    if not out["could_not_read"]:
        return []
    return ["", f"## {s('not_read', out['lang'])}", ""] + [f"- {item['name']}: {item['text']}" for item in out["could_not_read"]]


def _footer(out: dict[str, Any]) -> list[str]:
    read = sum(1 for src in out["sources"] if src["points"])
    return ["", "---", "", s("footer", out["lang"], date=out["date"], read=read, missed=len(out["could_not_read"]))]


# ---------------------------------------------------------------- summarise-feeds


def run_feeds(ctx: Any, lang: str) -> dict[str, Any]:
    template = "summarise-feeds"
    limits = LIMITS[template]
    out = _base(ctx, template, lang)
    urls = _urls(ctx.inputs.get("urls"))
    if not ctx.workspace.roots() and not urls:
        ctx.stop("Name the folder that holds the feed files: --folder PATH")
    feeds, ignored = sources.feed_files(ctx)
    out["given"] = [name for name, _ in feeds] + urls
    if ignored:
        ctx.note(s("n_ignored", lang, n=ignored))
    if not out["given"]:
        ctx.note(s("n_no_feeds", lang), incomplete=True)
        return _finish(ctx, out, "")
    instruction = (f"Below is the text of one feed. Write 2 to {limits['points']} short bullet points in {s('language', lang)} "
                   "on what its items say. One line each, starting with \"- \". Only what the text says: no introduction, no advice.")
    stopped = False
    for index, (name, info) in enumerate(feeds):
        if index >= limits["sources"]:
            _missed(out, name, "limit")
            continue
        if stopped:
            _missed(out, name, "budget")
            continue
        text, reason = sources.read_text(ctx, info)
        if text is not None:
            text, reason = sources.feed_text(name, text)
        if reason:
            _missed(out, name, reason)
            continue
        excerpt = text[:limits["excerpt"]]
        points = _ask_points(ctx, instruction, "FEED TEXT", excerpt, limits["points"])
        if points is None:
            stopped = True
            _missed(out, name, "budget")
            continue
        out["sources"].append({"name": name, "points": points, "sent_chars": len(excerpt)})
    getter = fetch.fetcher()
    for url in urls:
        try:
            getter.fetch(url, sources.MAX_FILE_BYTES)
        except fetch.NeedsSandbox:
            _missed(out, url, "sandbox")
    done = [src for src in out["sources"] if src["points"]]
    if len(done) >= 2 and not stopped:
        out["overview"] = _overview(ctx, done, lang)
    lines = [f"# {out['title']} · {out['date']}"]
    if out["overview"]:
        lines += ["", f"## {s('overview', lang)}", "", out["overview"]]
    for src in out["sources"]:
        lines += ["", f"## {src['name']}", ""] + [f"- {p}" for p in src["points"]]
    lines += _not_read_block(out) + _footer(out)
    return _finish(ctx, out, "\n".join(lines) + "\n")


def _overview(ctx: Any, done: list[dict[str, Any]], lang: str) -> str:
    """One everyday call for two or three sentences across the sources. Optional: the digest stands without it."""
    listed = "\n".join(f"- {p}" for src in done for p in src["points"])[:6000]
    prompt = (f"Below are points taken from {len(done)} feeds. In {s('language', lang)}, write 2 or 3 plain sentences on what "
              f"stands out across them. No list, no heading. Use only these points.\n\nPOINTS:\n{listed}")
    if not _can_ask(ctx, len(prompt) + OVERVIEW_TOKENS):
        return ""
    try:
        reply = ctx.llm.ask([{"role": "user", "content": prompt}], task="everyday", max_tokens=OVERVIEW_TOKENS)
    except ctx.llm.BudgetExceeded:
        return ""
    text = " ".join(reply.text.replace("#", "").split())
    if not text or len(text) > OVERVIEW_MAX_CHARS:
        ctx.note(s("n_no_overview", lang))
        return ""
    return text


# ---------------------------------------------------------------- summarise-folder-changes


def run_changes(ctx: Any, lang: str) -> dict[str, Any]:
    template = "summarise-folder-changes"
    limits = LIMITS[template]
    out = _base(ctx, template, lang)
    if not ctx.workspace.roots():
        ctx.stop("Name the folder to watch: --folder PATH")
    base, record = sources.baseline(ctx)
    current = sources.snapshot(ctx)
    sources.save_candidate(ctx, record, current, out["date"])
    out.update(new=[], changed=[], deleted=[], since=str(record.get("baseline_at") or ""), first_run=base is None)
    if base is None:
        ctx.note(s("n_first", lang, n=len(current)))
        return _finish(ctx, out, "")
    delta = sources.diff(base, current)
    out.update(new=delta["new"], changed=delta["changed"], deleted=delta["deleted"])
    out["given"] = delta["new"] + delta["changed"]
    if not out["given"] and not delta["deleted"]:
        ctx.note(s("n_no_changes", lang))
        return _finish(ctx, out, "")
    by_name = dict(sources.listing(ctx))
    instruction = (f"Below is the start of one file. Write 1 to {limits['points']} short bullet points in {s('language', lang)} "
                   "saying what the file holds. One line each, starting with \"- \". Only what the text says.")
    stopped = False
    for index, name in enumerate(sorted(out["given"], key=lambda n: (-current[n][1], n))):
        info = by_name.get(name)
        if index >= limits["sources"]:
            _missed(out, name, "limit")
        elif stopped:
            _missed(out, name, "budget")
        elif info is None:
            _missed(out, name, "unreadable")
        else:
            text, reason = sources.read_text(ctx, info)
            if text is not None and not text.strip():
                text, reason = None, "empty"
            if text is None:
                _missed(out, name, reason)
                continue
            excerpt = text[:limits["excerpt"]]
            points = _ask_points(ctx, instruction, "FILE TEXT", excerpt, limits["points"])
            if points is None:
                stopped = True
                _missed(out, name, "budget")
            else:
                out["sources"].append({"name": name, "points": points, "sent_chars": len(excerpt)})
    by_source = {src["name"]: src for src in out["sources"]}
    since = out["since"] or s("counts_first", lang)
    lines = [f"# {out['title']} · {out['date']}", "",
             s("counts", lang, new=len(delta["new"]), changed=len(delta["changed"]), deleted=len(delta["deleted"]), since=since)]
    for key in ("new", "changed"):
        shown = [name for name in delta[key] if name in by_source]
        if shown:
            lines += ["", f"## {s(key, lang)}"]
            for name in shown:
                lines += ["", f"### {name}", ""] + [f"- {p}" for p in by_source[name]["points"]]
    if delta["deleted"]:
        lines += ["", f"## {s('deleted', lang)}", ""] + [f"- {name}" for name in delta["deleted"]]
    lines += _not_read_block(out) + _footer(out)
    return _finish(ctx, out, "\n".join(lines) + "\n")


# ---------------------------------------------------------------- daily-brief


def run_brief(ctx: Any, lang: str) -> dict[str, Any]:
    template = "daily-brief"
    out = _base(ctx, template, lang)
    try:
        from muse.skills import chores

        facts = chores.brief_facts(os.environ, out["date"], lang)
    except Exception as exc:  # the home's own records are unreadable: say so, do not guess
        ctx.event("brief_unavailable", error=type(exc).__name__)
        facts = {"day": "", "sections": [{"name": s("title_brief", lang), "lines": [], "ok": False}]}
    out["day"] = facts["day"]
    out["given"] = [section["name"] for section in facts["sections"]]
    for section in facts["sections"]:
        if section["ok"] and section["lines"]:
            out["sources"].append({"name": section["name"], "points": [str(line)[:MAX_POINT_CHARS] for line in section["lines"]][:LIMITS[template]["points"]],
                                   "sent_chars": 0})
        else:
            _missed(out, section["name"], "unavailable")
    listed = "\n".join(f"{src['name']}: " + "; ".join(src["points"]) for src in out["sources"])[:LIMITS[template]["excerpt"]]
    if out["sources"]:
        prompt = (f"These are counts of what a personal AI helper did on {out['day']}. In {s('language', lang)}, write 2 plain "
                  f"sentences saying what stands out. Use only these numbers; add none of your own. No list.\n\nCOUNTS:\n{listed}")
        if _can_ask(ctx, len(prompt) + OVERVIEW_TOKENS):
            try:
                reply = ctx.llm.ask([{"role": "user", "content": prompt}], task="light", max_tokens=OVERVIEW_TOKENS)
                text = " ".join(reply.text.replace("#", "").split())
                for src in out["sources"]:
                    src["sent_chars"] = len("; ".join(src["points"]))
                if text and len(text) <= OVERVIEW_MAX_CHARS and digit_runs(text) <= digit_runs(listed):
                    out["overview"] = text
                else:
                    ctx.note(s("n_no_overview", lang))
            except ctx.llm.Error as exc:  # the counts stand on their own: a held or failed call costs only the two sentences
                ctx.event("overview_skipped", reason=type(exc).__name__)
    lines = [f"# {out['title']} · {out['date']}", "", s("brief_for", lang, day=out["day"] or "?")]
    if out["overview"]:
        lines += ["", out["overview"]]
    for src in out["sources"]:
        lines += ["", f"## {src['name']}", ""] + [f"- {p}" for p in src["points"]]
    lines += _not_read_block(out) + _footer(out)
    return _finish(ctx, out, "\n".join(lines) + "\n")


RUNNERS = {"summarise-feeds": run_feeds, "summarise-folder-changes": run_changes, "daily-brief": run_brief}
