---
name: recurring-chore
description: Turns one sentence ("Every Monday 08:00, summarise the feeds in ~/feeds into ~/Documents/Wardmuse") into a scheduled chore, and does the chore's work each time it is due. Use it for digests that should arrive without being asked.
license: See the repository licence
metadata:
  author: Wardmuse
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: light
  privacy_default: standard
  budget:
    requests: 40
    tokens: 120000
    seconds: 600
    paid_usd: 0
  needs:
    files.read: [chosen, data]
    files.write: [data, output]
    net: []
  writes: true
  check: check
---
# Recurring chores

Say what you want done and when, in one sentence, in English or Traditional Chinese. Wardmuse shows what it understood (what, when, where the result goes, the budget, what it needs to read and write) and asks once. After that the chore runs by itself and leaves a dated Markdown file for you to approve. Use `muse chores add "<sentence>"`.

The three chores it can do:

1. **Summarise feeds** (`summarise-feeds`). Reads the feed files you keep in a folder (RSS, Atom, JSON Feed, or text and HTML exports) and writes a few points for each, with a short overview. This version cannot fetch web addresses: that needs Wardmuse's sandbox, which is not built yet. Addresses in your sentence are kept with the chore and listed in every digest as "not read".
2. **Summarise what changed in a folder** (`summarise-folder-changes`). Notes which files are new, changed or gone since the last run and says in a few points what each new or changed file holds. Wardmuse keeps no old copies of your files, so it describes a changed file as it is now, not what was edited. The first run only records the starting point.
3. **Daily brief** (`daily-brief`). Counts of what Wardmuse did the day before on this home: requests, background jobs, chores, cards. If part of that cannot be read, the brief says which part.

How it works:

- The sentence is read by fixed rules first. Only when the rules cannot tell how often or which chore, one small model call may propose it; the proposal goes through the same rules, can never name a folder, and is marked so you can check it. Anything still unclear comes back as a question.
- Each source gets one small ("light") model call on a short excerpt, never the whole file. The overview is one more call. Everything goes through Wardmuse's router under the chore's privacy class.
- Before you see a digest, Wardmuse checks that every source it was given has at least one point or is listed under "could not read", that the points are short and in the right language, that no more than the budgeted excerpt of any file was sent, and that the dated file name is new.
- Each template has a default budget, shown before the first run (for feeds: up to 40 requests, nothing paid). A run that would exceed it stops, keeps what it has, and says which sources were left out.
- The result is a proposed file. It is saved only after you approve it: on a terminal you are asked; when the chore runs on its schedule, a "Save a file" card waits for your answer.

It keeps, in Wardmuse's own folder, the names, sizes and times of the files in a folder it watches for changes: no file contents.

Inputs: `mode` (`understand` or `run`); for `understand`: `sentence`; for `run`: `template`, `lang`, and optionally `date`, `urls`, `chore`, `run_id`, `last_ok_run` (set by `muse chores`).
