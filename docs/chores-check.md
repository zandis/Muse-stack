# Chores check: are the skills right on free allowances?

`scripts/chores_check.py` runs each shipped skill many times through the real skill runner and the real router, on free allowances only, and reports how often a run is **right**. It answers gate G1 in [the execution plan](spec/04-execution-plan.md).

Since 2026-11-13 the gate counts runs that are **right by the expected facts**, not runs that passed the skill's own check. The full real run that day showed why: research-report passed its own check in 82% of runs (over the 80% line) while only 41% of its reports were right. The own-check rate is still printed, as a second line, with the gap called out.

It needs a machine with real provider keys and a running router. Without one, only `--check-tasks` and the tests work.

## Before you run it: update the live router

The first real run (2026-11-13) used current skills against an **old router build**: no output floor above the light ceiling, no `x-muse-answer`, no `x-muse-reasoning`. The fixes below are half on the router side. Update and restart the router before re-running, or the numbers will repeat the old failures. The check now warns loudly when the router answers like an old build:

```
WARNING: your router is older than these skills: it did not send the newer headers (x-muse-min-output / x-muse-reasoning / x-muse-answer) where a current build would. Update it and restart it, then run this again.
```

It can only tell when a call landed on a deployment where a current build always says something (a gpt-oss deployment on a light or everyday call, or an empty answer). The report's `router` says `current`, `old` or `unknown`.

## Run it

On the machine with the keys, with the router up and current (`muse up`):

```bash
# 0. Sends nothing: are the task files and fixtures consistent?
python scripts/chores_check.py --check-tasks

# 1. A cheap sample first: 5 tasks per skill, 10 runs in all, keeping what went wrong.
python scripts/chores_check.py --sample 5 --keep-failures

# 2. The full check, capped well below the day's free allowance.
python scripts/chores_check.py --max-requests 400 --keep-failures

# Later, or the next day, after deferrals or the cap: finish what did not complete.
python scripts/chores_check.py --resume --max-requests 400
```

Each command prints what it will send and waits for `y`. Add `--yes` for an unattended (nightly) run.

The worst case is each skill's own budget times the runs: 12 requests and 60,000 tokens for `files-answer`, 40 and 150,000 for `research-report`. It is a ceiling, not an estimate. Measured on 2026-11-13 (the old router): files-answer 2.2 requests and 2,313 tokens a run; research-report 11.7 requests and 8,916 tokens a run. research-report now makes one extraction call per sub-question (offline: 5.6 requests a run instead of 8.3).

Options: `--only files-answer`, `--only-kinds date,numeric`, `--max-requests N` (a run is not started when its budget could take the batch over N; the report prints the requests used against the cap), `--runs N`, `--sample K`, `--seed S`, `--privacy private`, `--delay SECONDS` (default 2), `--max-wait SECONDS` (longest `Retry-After` to sit out, default 60), `--stop-after N` (stop a skill after N deferred runs in a row, default 5), `--keep-failures`, `--keep-outputs`, `--report PATH`, `--csv PATH`, `--record DIR`, `--replay DIR`.

## Offline

`--record DIR` writes a cassette for each exchange with the router while the check still uses that router. `--replay DIR` answers the same calls from those cassettes: the router is not contacted, and no provider quota is used. Record on a machine that has the keys, review the files (they hold response text, never the prompt and never a key), then replay that directory for a later run of the same sample. The two flags together are refused. The workflow, and how a replay can guard a no-regression check, is in [testing/fidelity-and-replay.md](testing/fidelity-and-replay.md).

## What it does to stay free and polite

- Every run uses the runner's own app `skills`, which may never pay, and a money budget of 0. `x-muse-allow-paid` is never sent.
- **If the router reports any cost above zero, the check stops at once**, prints a loud message and exits with code 3.
- One run at a time, with a pause between runs. A `Retry-After` up to `--max-wait` is sat out; a longer one ends the run as **deferred**. Five deferred runs in a row stop that skill.
- `--max-requests` keeps a nightly batch well below the day's allowance. The full set on 2026-11-13 used 922 requests and ran the free allowances dry near the end: the last ten runs were answered by OpenRouter alone.
- At the start and the end the report records the day's allowance per connected provider from the local ledger (`muse status`: state, requests today, what is left when known). A provider that went from `ok` to `exhausted` or `cooling` during the batch is named, because runs after that point had fewer providers.

## What the numbers mean

| Line | Meaning |
|---|---|
| right by expected facts | **The gate.** Each task lists facts that must appear in the answer and the file each should cite; a not-in-the-files task expects the skill to say so. |
| passed own check | The runner returned `done` or `partial` and the skill's own check passed. Second line, with "it overstates the right-rate by N points", "passed but wrong" and "right but failed its own check". |
| statuses | done / partial / needs_choice / failed counts, then empty answers, recoveries (an empty answer asked again with more room that then answered) and still unanswered. |
| right by empty answers | The right-rate of runs with 0, 1-2 and 3+ empty answers: on 2026-11-13, 9/13, 11/19 and 7/34. |
| requests per step | research-report: plan, gather (embeddings), extract, synthesise. |
| slowest calls | The slowest calls of the batch: task, provider, milliseconds, and the total call time against the time spent waiting for `Retry-After`. A long call includes the router's own work inside that one request (LiteLLM's retry, `num_retries: 1`, and fallbacks to other deployments); the client cannot split that further. Per-run records hold every call's provider, task, milliseconds and status. |
| router build seen | current / old / unknown per run. |
| failed | `check-failed` (reasons as codes: `uncited`, `quote-not-found`, `number-not-quoted`, `quote-size`, `unknown-source`, `not-read`, `unanswered`, `off-subject`, `missing-part`, `length`, `language`, `coverage`, `empty`), `budget`, `router-error`, `privacy-refused`, `skill-error`. |
| 95% interval | Wilson score interval. With 50 runs and 40 right it is about 67% to 89%. |

```
G1: at least 80% of completed runs RIGHT by the expected facts with nothing spent: no (27 of 66)
Own check (second line, not the gate): 54 of 66 passed (81.8%); it overstates the right-rate by 40.9 points; passed but wrong 30, right but failed its own check 3. By the own check alone this skill would have looked like it met the line; it did not.
```

### Expected facts are typed

A fact can be a plain list of accepted strings (`any`), a `number`, a `regex`, or a **typed** value that takes every way of writing it and no other value:

| Type | Example | Accepted |
|---|---|---|
| `{"date": "2031-04-03"}` | fa-025 | 2031-04-03, 2031/4/3, 2031.04.03, 3 April 2031, 03 Apr 2031, April 3, 2031, 2031年4月3日, 民國120年4月3日. `"year_optional": true` also takes "3 April" / "4月3日". |
| `{"time": "08:40"}` | rr-021 | 08:40, 8:40, 8:40 am, 8.40am, 上午8點40分, 8點40分. 13:15 also takes 1:15 pm, 下午1點15分 and a bare 1:15. |
| `{"amount": 1450}` | rr-001 | 1,450, 1450.00, US$1,450, NT$1,450, 1,450 元. |
| `{"count": 3, "unit": "year"}` | fa-027 | 3 years, three years, 3-year, 3 年, 三年 (兩年 for 2). |

97 facts of the shipped tasks were converted (15 dates, 10 times, 18 counts, 55 amounts, one by hand: rr-028's "10:30" is 22:30 in the handbook), each only where the typed fact accepts every form the old list did; the check that every expected fact is in its fixture still holds.

### --keep-failures

Writes one line per wrong or failed run to `$MUSE_HOME/chores-failures.jsonl` (mode 0600): the answer text, its claims (what each says, the exact quote and its passage number and source label: only the skill's own quotes, never a passage or a prompt), and for each expected fact that was missing its kind and accepted forms. Nothing is kept by default. With it, a miss like fa-025 shows whether the answer had the wrong date or the right date in a form the scorer did not list.

## What the first real runs showed (2026-11-13)

**Sample** (`--sample 5`, 10 runs, 74 requests, US$0): files-answer own 5/5, right 4/5 (fa-025's date was not in any accepted form; output not kept). research-report own 3/5, right 0/5: 62 requests, 21 **empty** answers ("reasoning used the whole output"); two reports passed their own check while saying the files did not answer, after empty extraction calls; one failed on an uncited sentence; the Chinese question over English files failed the word-overlap check.

**Full set** (133 runs, 922 requests, 743,426 tokens, US$0, 78 minutes): files-answer right 65/67 (zh 28/28, cross-language 6/6; wrong-but-passed fa-027, one of two parts answered, and fa-035, the size of Flat 4B "answered" from the purifier's 42 square metres). research-report own 54/66, right 27/66; 774 requests (11.7 a run), 172 empty answers (22% of calls); right-rate falls from 9/13 with no empty answer to 7/34 with three or more.

Causes, as found in the code:

1. **Hidden reasoning used the light allowance.** Task `light` caps output at 300 tokens. The gpt-oss deployments had an output floor of 256, below the ceiling, so it never applied, and reasoning used all 300.
2. **An empty answer read as "nothing found".** research-report listed the sub-question under "what the sources did not answer", and its own check passed.
3. **The word-overlap citation check** could not work across languages and was weak within one.
4. **The scorer was strict on formats**, not only on values.

## The fixes

- **Router**: the gpt-oss floor is 1024 (an observation, not provider documentation); `reasoning_effort: low` on light and everyday calls to Groq gpt-oss and Cerebras gpt-oss-120b, where the providers document it and LiteLLM passes it on (`x-muse-reasoning: low`). See the README.
- **Empty answers are failures**: an empty answer is asked once more with four times the room one tier up; still empty, it is `Unanswered`. research-report marks those passages NOT READ, lists the sub-question under "Not done" and its check does not pass; files-answer says the question was not answered, never that the files lack it.
- **Claims are verbatim quotes** checked on this computer: the quote must be in the passage it names, every number stated must be in the quote, every sentence must cite a claim. Works across languages.
- **files-answer**: an answer must come from a file that mentions what the question names (Flat 4B, INV-3109), and a question asking for two things needs a claim for each or must say what is missing (from fa-035 and fa-027).
- **research-report**: one extraction call per sub-question on the everyday tier instead of many light calls.
- **Harness**: right-by-facts gate, typed facts, `--keep-failures`, the old-router warning, `--max-requests`, `--only-kinds`, per-call timing and the allowance at both ends.

### Offline experiments (fake models, the shipped task set, embeddings off)

| | files-answer right | research-report right | research requests a run |
|---|---|---|---|
| model that quotes every expected fact it is shown, before | 62/67 | 55/66 | 8.3 |
| the same, now | 62/67 | 57/66 | 5.6 |
| the same with 22% of light calls empty, before | 62/67 | 52/66 (own check 66/66) | 8.3 |
| the same with 22% of light calls empty, now | 62/67 | 57/66 | 5.9 |
| naive extractive model (copies the sentences that share most words with the question), before | 37/67 (own 67/67) | 7/66 (own 65/66) | 8.5 |
| naive, now | 37/67 (own 65/67) | 6/66 (own 65/66) | 5.8 |

What this says: the pipeline can be right in about 93% (files-answer) and 86% (research-report) of the tasks with a perfect reader; most of the rest are passages the search never found (lexical search cannot find English passages for a Chinese question with embeddings off: research-report cross-language 2/5). Empty answers no longer turn into silent passes. And the quote rule does **not** close the gap for a model that quotes true but irrelevant sentences: those quotes are real, so the check passes them. That is what the gate change is for.

## Files

- `$MUSE_HOME/chores-report.json`: options, per-skill summary, one record per run (with per-call provider, task and milliseconds), router build, allowance at start and end, the verdict. `--resume` keeps the runs that passed or failed and runs the rest.
- `$MUSE_HOME/chores-runs.csv`: one line per run, appended, so nights can be compared. Compare only lines with the same `set`.
- `$MUSE_HOME/chores-failures.jsonl`: only with `--keep-failures`; see above.

The report and CSV hold no document text, prompts, model answers, check-reason wording or keys: ids, counts, provider names, milliseconds and short codes. `--keep-outputs` adds answers, notes and reasons to the JSON report.

Exit codes: 0 every skill met the G1 line; 1 one did not, or the router was unreachable; 2 usage problem; 3 stopped because something was charged.

## Limits

- **The fixtures are small and clean**: fourteen short documents. Real folders are harder to search.
- **The expected facts are lenient in one way**: an answer that mentions the right value in a wrong sentence scores as right. Typed facts are strict on values, so a wrong date in another form is still wrong.
- **The router-build check can say `unknown`** for a whole batch answered by models without a floor or reasoning setting.
- **Allowances change.** Compare nights with the CSV. The full run of 2026-11-13 is in `docs/reviews/gate1-full-2026-11-13.md` (about 900 requests, and the free allowances ran out). Repeat a recorded sample with `--replay` so a later check of the same tasks spends nothing.
