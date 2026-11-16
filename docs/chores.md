# Chores

A chore is something the helper does on a schedule, added as one sentence and one confirmation. This page describes the first form (work package A7): the `recurring-chore` skill, the schedule store, and `muse chores`. The skill format and the runner are in `docs/skills.md`.

```bash
muse chores add "Every Monday 08:00, summarise the feeds in ~/feeds into ~/Documents/Wardmuse"
muse chores add "每個工作天下午三點半整理 ~/notes 的變動"
muse chores list                 # each chore, its next run, last result and budget
muse chores show <id>            # what, when, where the result goes, budget, what it needs, history
muse chores run-now <id>         # once, now; asks before writing (--yes writes)
muse chores pause <id>           # resume <id>; edit <id> "<new sentence>"; rm <id>
muse chores tick                 # one pass; a timer calls this every minute
```

## What is and is not built

- **No web fetching.** A skill that reaches the internet itself is refused until Wardmuse's sandbox exists. So the feed chore reads feed **files** you keep in a folder (RSS, Atom, JSON Feed, text or HTML exports). Web addresses in a sentence are kept with the chore and listed in every digest as "not read". The fetch step sits behind one interface (`skills/recurring-chore/fetch.py`) marked "needs the sandbox".
- **No daemon.** Nothing runs by itself until a timer calls `muse chores tick` (below).
- **Rules first.** The sentence is read by fixed rules. A model is asked only when the rules cannot tell how often or which chore, and what it proposes goes through the same rules. Quality on real small models is not measured yet (work package A9).
- **Every file needs your yes.** A run proposes one new dated Markdown file. It is written only after you agree, as a new file, never over an existing one.

## Adding a chore

`muse chores add "<sentence>"` shows what was understood, then asks once:

```
What: Summarise the feed files in /home/ann/feeds
When: Every Monday at 08:00 (Asia/Taipei time)
Result: A new dated Markdown file in /home/ann/Documents/Wardmuse, saved only after you approve it
Budget: up to 40 requests, nothing paid
Needs: Read the files in /home/ann/feeds; propose one new file in /home/ann/Documents/Wardmuse each run (each is saved only after you approve it)
Add this chore? [y/N]
```

If something is missing or not supported, Wardmuse asks instead of guessing, and adds nothing (exit code 4). `--folder PATH` (repeatable) and `--out PATH` name folders beside the sentence, for "these feeds". `--requests N` changes the budget, `--privacy` the privacy class, `--no-model` forbids the one model call, `--yes` confirms without the question, `--json` prints what was understood.

### What a sentence can say

| | English | 繁體中文 |
|---|---|---|
| Every day | every day, daily, each day, every morning / evening / night, once a day | 每天、每日、天天、每晚 |
| Working days | every weekday, on weekdays, every working day, Monday to Friday | 每個工作天、每個工作日、平日、週一到週五 |
| Days of the week | every Monday, every Mon and Thu, Fridays, every weekend, weekly on Wednesday | 每週一、每周三、每星期五、每禮拜日、每週一、三、五、每週末 |
| A day of the month | every month on the 1st, on the 15th of every month, monthly on the 3rd, on the last day of every month | 每月1號、每個月15日、每月三十一號、每月最後一天、每月底 |
| Spare allowance | when there is allowance to spare, whenever there's spare allowance, with leftover allowance | 有剩餘額度時、額度有剩的時候 |
| Time | 08:00, 8:30, 8am, 3:30 pm, at 8, at 8 in the evening, noon, midnight | 早上8點、上午十點半、下午三點半、晚上9點15分、中午、凌晨2點、下午3:30 |
| Chore | feeds, RSS, news, newsletters, articles → feeds; what changed, new files → folder changes; brief, daily brief, what Wardmuse did → daily brief | 訂閱、新聞、文章；變動、變更、異動、新的檔案；簡報、日報、做了什麼 |
| Folders | `~/feeds`, `/data/in`, `Documents/Wardmuse` (under your home folder), `./here`; a name with spaces or non-ASCII characters in quotes: `"/data/my feeds"`, 「/data/我的訂閱」 | |
| Destination | into / to / save it in `<folder>` | 存到／放到／寫到 `<folder>` |
| Language of the result | the language of the sentence; "in English", "in Chinese" | 用英文、用中文 |

No time given: 08:00 is assumed and shown ("morning" 08:00, "afternoon" 14:00, "evening" 18:00, "night" 21:00). A spare-allowance chore with no schedule runs at most once a day, at any time. "The 31st" runs on the last day of a shorter month.

Not supported, so Wardmuse asks: every other week, every N days, hourly, twice a day, yearly; two times of day; a day of the week together with a day of the month; a single run ("tomorrow"); a weekday without "every" is read as weekly and says so. Not understood at all: Windows paths, times written as words ("at eight"), "quarter past".

## Running: `muse chores tick`

`muse chores tick` is one short pass:

1. Cards answered since the last pass are settled: an approved result is written, a declined or expired one is dropped.
2. Every chore whose time has come gets exactly one queued run, and its next run moves forward. This is one database transaction, so two timers firing at once cannot queue a run twice. A chore that was due several times while the home was off runs once.
3. Queued runs whose time has come are run, at most five a pass. Nothing runs while Wardmuse is paused (`muse pause`); due chores are still queued.

Call it every minute. With systemd, as your own user:

```ini
# ~/.config/systemd/user/muse-chores.service
[Service]
Type=oneshot
ExecStart=%h/.local/bin/muse chores tick

# ~/.config/systemd/user/muse-chores.timer
[Timer]
OnCalendar=minutely
Persistent=true
[Install]
WantedBy=timers.target
```

`systemctl --user enable --now muse-chores.timer`. Or cron: `* * * * * muse chores tick`. The app will call the same function. `muse queue run` is separate: it drains prompt jobs added with `muse queue add`; chores keep their own runs in `MUSE_HOME/chores.sqlite`.

**The result of a run by tick.** Nobody is at a terminal, so the run creates a "Save a file" card (the same cards the home control API serves) and the digest waits, owner-only, in `MUSE_HOME/chores-pending/`. When the card is approved, the next tick writes the file. Declined or expired (24 hours): the pending result is deleted and the run is recorded as "not approved". `--wait-card SECONDS` makes tick wait that long for the answer before moving on. "Always allow this" on a card is not acted on yet; that needs standing permissions.

**Times.** Schedules are in wall-clock time of `MUSE_TZ` (the zone rationing uses), stored with the chore. 08:00 stays 08:00 across a daylight-saving change. A time that does not exist on the spring-forward day runs just after the gap; a time that happens twice runs once.

**Spare-allowance chores** are sent as the background app `skills-background`. The router's existing rules decide when background work may go: it yields to the app you are using, keeps a reserve, paces a day's allowance, and waives both shortly before an allowance resets, so expiring allowance is used. When the router holds a run back it says when to come back; the run is queued for that time (at most six hours out) and is not a failure. If it is still waiting when the chore's next occurrence comes round, it is skipped and the list says "no allowance was free". There is no separate "run before this allowance resets" hint: the router already knows the reset times, and a chore does not.

**When something goes wrong.** The router cannot be reached: the run is tried again after 5 and 10 minutes, then recorded as failed. The digest fails its own check twice (for example, it missed two sources): the run is recorded as "Couldn't finish: the digest missed 2 source(s)" and what it got is kept as a draft, shown by `muse chores show`. A folder the chore reads is gone: the run fails and says so.

## The three chores

| Template | What it reads | Model use | Default budget |
|---|---|---|---|
| `summarise-feeds` | Feed files in the named folders, at most 30 a run, the first 6,000 characters of each | One `light` call per file, one `everyday` call for the overview | up to 40 requests, nothing paid |
| `summarise-folder-changes` | Names, sizes and times of every file; the first 3,000 characters of at most 12 new or changed text files | One `light` call per file | up to 20 requests, nothing paid |
| `daily-brief` | Wardmuse's own counts for the day before: requests, background jobs, chores, cards | None needed; one `light` call for two sentences | up to 4 requests, nothing paid |

A run that would exceed its budget stops, keeps what it has, lists the rest under "Could not read", and is marked partial. Folder changes: the first run only records the starting point; Wardmuse keeps no old copies of files, so a changed file is described as it is now. The record of names, sizes and times is in `MUSE_HOME/skills-data/recurring-chore/`; it holds no file contents. A run whose digest was not delivered does not swallow the changes it saw.

**The check**, before a digest is offered: every source it was given has at least one point or is listed under "Could not read"; each source has at most 5 points (3 for folder changes) of at most 300 characters; the points are in the language asked for; no more than the budgeted excerpt of any source was sent; the dated file name is new; the proposed file is exactly the checked digest; for the daily brief, the summary sentences hold no number that is not in the counts. A failed check is retried once with every step one tier stronger.

## What is stored

| Place | Holds |
|---|---|
| `MUSE_HOME/chores.sqlite` (owner-only) | Your sentences, folder names, schedules, budgets; per run: status, times, counts, a card id. Never file contents, prompts or answers. |
| `MUSE_HOME/chores-pending/` (owner-only) | A digest waiting for your answer, or a draft that failed its check. Removed when answered or when the chore is deleted. |
| `MUSE_HOME/skills-data/recurring-chore/` | For folder changes: file names, sizes and times. |

## Related: set up my services

`muse skills run setup-services [--input goal="long documents"] [--input lang=zh-TW]` says what is connected and the one service to add next, with its steps (work package A6). It reads the catalogue and whether keys are present; it uses no model unless a goal matches none of its words, and then at most one small call to phrase a reason. Its description (`muse skills show setup-services`) lists what it checks.
