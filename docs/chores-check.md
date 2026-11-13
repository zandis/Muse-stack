# Chores check: do the skills pass on free allowances?

`scripts/chores_check.py` runs each shipped skill many times through the real skill runner and the real router, on free allowances only, and reports how often a run passes. It answers gate G1 in [the execution plan](spec/04-execution-plan.md): "at least 80% of runs pass their own check on free allowances with nothing spent".

It needs a machine with real provider keys and a running router. Without one, only `--check-tasks` and the tests work.

## Run it

On the machine with the keys, with the router up (`muse up`):

```bash
# 0. Sends nothing: are the task files and fixtures consistent?
python scripts/chores_check.py --check-tasks

# 1. A cheap sample first: 5 tasks per skill, 10 runs in all.
python scripts/chores_check.py --sample 5

# 2. The full check: every task once (67 for files-answer, 66 for research-report).
python scripts/chores_check.py

# Later, or the next day, after deferrals: finish what did not complete.
python scripts/chores_check.py --resume
```

Each command prints what it will send and waits for `y`. Add `--yes` for an unattended (nightly) run.

| Command | Runs | Worst case |
|---|---|---|
| `--sample 5` | 10 | 260 requests, 1,050,000 tokens |
| `--sample 50` (exactly the 50 runs per skill the gate names) | 100 | 2,600 requests, 10,500,000 tokens |
| no `--sample` | 133 | 3,444 requests, 13,920,000 tokens |

The worst case is each skill's own budget times the runs: 12 requests and 60,000 tokens for `files-answer`, 40 and 150,000 for `research-report`. It is a ceiling, not an estimate. By design, a `files-answer` run that passes first time makes 1 chat call and up to 3 embedding calls; a `research-report` run makes up to 17 (1 plan, up to 3 embedding, up to 12 extract, 1 heavy), and a failed check doubles that once. How much real models use has not been measured before the first run on the VM; the report says.

Useful options: `--only files-answer`, `--runs N` (repeat every task N times), `--seed S` (which sample), `--privacy private`, `--delay SECONDS` (pause between runs, default 2), `--max-wait SECONDS` (longest `Retry-After` to sit out, default 60), `--stop-after N` (stop a skill after N deferred runs in a row, default 5), `--keep-outputs`, `--report PATH`, `--csv PATH`.

## What it does to stay free and polite

- Every run uses the runner's own app `skills`, which may never pay, and a money budget of 0. `x-muse-allow-paid` is never sent.
- **If the router reports any cost above zero, the check stops at once**, prints a loud message and exits with code 3. No further run is started.
- One run at a time, with a pause between runs.
- A `Retry-After` of up to `--max-wait` seconds is sat out (at most three times in a run). A longer one ends the run as **deferred**.
- After five deferred runs in a row for a skill, that skill's remaining tasks are not started: the free allowance is used up for now. `--resume` continues later.
- If the router cannot be reached twice in a row, it stops and says to start it.

## What the numbers mean

For each skill:

| Line | Meaning |
|---|---|
| planned / completed / deferred / not run | A run is **completed** when it ended with a result the skill could be judged on. **Deferred** runs were held back by the router (a free allowance ran out or was being paced); they are reported apart and count neither as passes nor as failures. **Not run** were never started. |
| passed own check | The runner returned `done` or `partial` and the skill's own check passed, after its one stronger retry if needed. This is what G1 counts. `partial` passes are counted here and also shown separately. |
| right by expected facts | The independent score. Each task lists facts that must appear in the answer and the file each should cite; a not-in-the-files task expects the skill to say so. It needs no model. |
| both | Passed its own check and was right. |
| 95% interval | The Wilson score interval for the rate. With 50 runs and 40 passes it is about 67% to 89%: fifty runs cannot tell 80% from 70% reliably. |
| failed | By category: `check-failed` (with the check's reasons as codes: `uncited`, `weak-overlap`, `unknown-source`, `length`, `language`, `coverage`, `empty`), `budget`, `router-error`, `privacy-refused`, `skill-error`. |
| deferrals, offered | How many times the router held a call back, and which choices its refusals carried (`wait`, `wait+handoff`, ...). |
| used | Requests, tokens and cost through the router, and which providers answered. Cost must be 0. |
| empty answers | Calls that returned no visible text because a reasoning model spent its output allowance (`x-muse-answer: empty-reasoning`). |
| by kind, by language | Own-check passes for lookup, multi-hop, numeric, date, not-found, cross-lang and long-file tasks, and for English and Traditional Chinese questions. |

Then the verdict line:

```
G1: at least 80% of completed runs passed their own check with nothing spent: yes (54 of 61)
```

It is `yes` when at least 80% of completed runs passed their own check and the total cost is exactly 0. If fewer than 50 runs completed for a skill, the line says so: the gate asks for 50. The overall line is `yes` only when every skill that ran is.

And the caveat, when it applies:

```
CAVEAT: the skill's own check passed answers that were wrong 9 time(s) by the expected facts; the own-check rate overstates how often it is right.
```

The own check is lexical: it proves that each statement shares words with the passage it cites, not that it answers the question. The expected-facts score is the plain measure of "is it right". Read G1 with both numbers.

## Files

- `$MUSE_HOME/chores-report.json`: the whole report: options, per-skill summary, one record per run, the verdict. `--resume` keeps the runs that passed or failed and runs the rest (deferred ones included).
- `$MUSE_HOME/chores-runs.csv`: one line per run, appended on every invocation, so nightly runs can be compared. `set` changes when a task or a fixture changes; compare only lines with the same `set`.

Neither file holds document text, prompts, model answers, the wording of check reasons, or any key: only ids, counts, provider names and short codes. `--keep-outputs` adds each run's answer, notes and check reasons to the JSON report; the test documents are fictional, so this is safe to share, but it is off by default so the habit carries over to real folders.

Exit codes: 0 every skill met the G1 line; 1 one did not, or the router was unreachable; 2 usage problem (including a broken task file); 3 stopped because something was charged.

## The task sets

See [evals/chores/README.md](../evals/chores/README.md): two folders of fictional documents (English and Traditional Chinese) and one `tasks.jsonl` per skill. Skills that are not installed are skipped with a note; add a task file for a new skill in the same shape.

## Limits

- **What the G1 line proves.** That, on this machine, on these days, with these keys, the skills' own checks passed in at least 80% of the runs that the free allowances let through, at no cost. It does not prove the answers were right (that is the expected-facts line), that an allowance is large enough for a day's real use (see the deferred count), or that it holds for other documents.
- **Deferred runs are left out of the rate.** A night where most runs are deferred can still print `yes` on a handful of completed runs; the line says how many, and the gate needs 50 per skill.
- **The fixtures are small and clean**: fourteen short documents, plain text and CSV, no scans, no PDFs, no messy exports. Real folders are harder to search.
- **The expected facts are strict in one way and lenient in another.** A right answer worded differently (a date in another format that the task does not list) scores as wrong; an answer that mentions the right number in a wrong sentence scores as right.
- **Cross-language tasks mostly fail the own check by design**: the check compares words, so an answer in one language citing a passage in another shares too few. They are in the set to show the size of that gap.
- **Free allowances change.** Providers change limits and models; compare nights with the CSV rather than trusting one run.
- **Not run here.** The harness has only been run against the fake router in the tests. No real provider has been called by it yet.
