# Skills

A skill is a packaged ability the helper can use: a folder with a `SKILL.md` and some Python. This page describes the first form (work packages A4 and A5): skills are plain Python run by a simple runner on the home, and they reach a model only through the router's OpenAI-compatible endpoint. The helper runtime with permissions, a sandbox and confirm cards (A11, A12) comes later and will wrap this runner; skills written as described here do not change.

```bash
muse skills list                       # every skill and whether it may run
muse skills show files-answer          # what it does and what it asks for, in plain words
muse skills approve my-skill           # read what one of your own skills asks for, then allow it
muse skills run files-answer --folder ~/Documents/notes --input question="When does the lease end?"
```

## What is and is not protected today

Read this before you add a skill of your own.

- **Keys.** A skill never receives a provider key and has no code path to a provider. `ctx.llm` posts to the local router with the runner's own app token; the router picks the service, rations it and adds the key.
- **Budget.** Every model call is counted against the job's budget before it is sent. A call that would exceed it is not sent.
- **Privacy.** Every call carries the job's privacy class. A step may make it stricter, never looser. A privacy refusal from the router ends the run; nothing is retried under a weaker class.
- **Files.** A skill is given a `Workspace`. Every path that goes through it is resolved, symbolic links included, and must lie inside a folder you named for this run; `..` is refused. Writes are proposals that are applied only after you agree, as new files only.
- **Approval.** One of your own skills runs only after you approved its exact bytes, and is refused again if any byte changes. What runs is the same bytes that were hashed.

What is **not** protected yet: a skill is Python code running in the runner's process, as you. The `Workspace` is a checked door, not a wall: code that ignores it and calls `open()` or opens a socket is not stopped, and it could read the environment of the process it runs in. A symbolic link swapped between the check and the read is not caught either. Real isolation (one process per tool, no environment, only the named paths, network off) arrives with the sandbox package. Until then: approve only code you trust, as you would any script. A skill that declares `net` hosts is refused outright, because nothing could hold it to that list.

## The folder

```
my-skill/
  SKILL.md        required
  skill.py        the entry module named in SKILL.md
  helpers.py      more modules; use relative imports: `from . import helpers`
```

Skills are found in two places:

| Place | State |
|---|---|
| `skills/` in the repository (override: `MUSE_SKILLS_DIR`) | Ready. Approved by being shipped. |
| `MUSE_HOME/skills/` (default `~/.muse/skills/`) | Not approved until `muse skills approve <name>` records its hash in `MUSE_HOME/skills-approved.json`. Changed since approval: refused until approved again. |

The folder name must equal `name`. A home skill may not reuse the name of a shipped one. A folder that contains a symbolic link, has more than 500 files or 20 MB, or fails validation is listed by `muse skills list` with the reason and cannot run.

**Content hash.** `sha256` over every file in sorted path order: an 8-byte length and the path, then an 8-byte length and the bytes. `__pycache__` folders and `.pyc`/`.pyo` files are left out, and are never read or written when the skill is imported: the runner imports the skill from the same bytes it hashed.

## SKILL.md

YAML frontmatter in the open Agent Skills shape, then the description people read before first use.

```markdown
---
name: files-answer
description: Answers a question from the person's own files, citing the passage behind every statement.
license: MIT                      # optional (open format)
metadata:                         # optional (open format); `author` is shown when you approve
  author: Ann Example
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: everyday
  privacy_default: standard
  budget:
    requests: 12
    tokens: 60000
    seconds: 180
    paid_usd: 0
  needs:
    files.read: [chosen]
    files.write: [data]
    net: []
  writes: false
  check: check
---
# Answer over my files

What it does, how, and what it cannot do. Shown before first use and by `muse skills show`.
```

Top level: `name` and `description` are required; `license`, `compatibility`, `metadata` and `allowed-tools` from the open format are accepted; `muse` is required. Any other top-level key is an error.

| Key under `muse:` | Required | Meaning |
|---|---|---|
| `version` | yes | `"1"`, `"0.2"` or `"0.2.1"` (quote it) |
| `entry` | yes | `<file>.py:<function>` inside the folder. The function receives one argument, the context. |
| `tier_default` | yes | `light`, `everyday`, `heavy` or `frontier`: the tier of a step that names none (see `muse rules`) |
| `privacy_default` | yes | `standard`, `private` or `local`: the class every call carries unless the run names another |
| `budget` | yes | `requests` (whole number, 1 or more), `tokens` (1 or more), `seconds` (above 0), `paid_usd` (0 or more; default 0: never pay) |
| `needs` | yes | What it touches. All three lists are required; use `[]` for none. |
| `writes` | yes | `true` exactly when `needs.files.write` lists `output` |
| `check` | yes | The name of the output check: a function in the entry module |

`needs` names roles, never paths. You bind them to real folders each run.

| Entry | Meaning |
|---|---|
| `files.read: [chosen]` | Read files in the folders named for this run (`--folder`). The skill cannot choose folders. |
| `files.read: [data]` or `files.write: [data]` | Keep its own working data in `MUSE_HOME/skills-data/<name>/`. This may include text copied from files it reads. |
| `files.write: [output]` | Propose new files in the output folder (`--out`, default `MUSE_HOME/skills-out/<name>/`). |
| `net: [host, ...]` | Hosts it wants to reach itself. Accepted by the format; refused by the runner until the sandbox exists. |

Unknown keys under `muse:`, `budget` or `needs`, a wrong type, a missing key, or a `name` that differs from the folder are errors. Every problem is reported at once.

## Writing a skill

```python
def run(ctx):
    question = ctx.inputs.get("question") or ctx.stop("Give a question.")
    texts = [ctx.workspace.read_text(info.path) for info in ctx.workspace.list()]
    reply = ctx.llm.ask([{"role": "user", "content": question + "\n\n" + texts[0]}], max_tokens=300)
    if reply.empty:
        ctx.note("The model returned nothing.", incomplete=True)
    return {"text": reply.text}


def check(output, ctx):
    return [] if output["text"].strip() else ["the answer is empty"]   # reasons; empty means passed
```

The context is the whole surface a skill should use:

| | |
|---|---|
| `ctx.inputs` | The inputs of this run (`--input key=value`) |
| `ctx.llm.ask(messages, *, task=None, privacy=None, max_tokens=None, json_schema=None)` | One chat call. Returns `text` plus the route facts: `provider`, `cost_usd`, `cache`, `min_output`, `answer` (`empty-reasoning` when the model produced no visible text), `task`, `handoff`. |
| `ctx.llm.embed(texts)` | Vectors through the `embed` alias |
| `ctx.llm.remaining()` | What is left of the budget |
| `ctx.llm.Deferred`, `.PrivacyRefused`, `.BudgetExceeded`, `.RouterError`, `.Error` | What a call can raise. Catch one only for a step the skill can do without; let the rest reach the runner. |
| `ctx.workspace.list(root=None)`, `.read_text(path)`, `.read_bytes(path)`, `.roots()` | Files in the folders named for this run |
| `ctx.workspace.propose_write(path, data, reason)` | A file the skill would like written. Nothing touches the disk. |
| `ctx.data_dir` | The skill's own working folder (needs `data`) |
| `ctx.note(text, incomplete=False)` | A plain sentence for the person. `incomplete=True` makes the result `partial`. |
| `ctx.stop(text)` | End the run as failed with one plain sentence |
| `ctx.event(kind, **facts)` | A progress fact for the app: numbers and short codes only |
| `ctx.attempt` | 1, or 2 on the stronger retry |

Never put text from a file, a prompt or an answer in a note, an event or an exception message. The runner drops long strings from events and reports a crash by its type and line only, but the rule is the skill's to keep.

## What the runner does

`muse.skills.run(skill, inputs, *, budget=None, privacy=None, on_event=None, apply_writes=False, folders=(), out_dir=None, task=None)` returns a `Result`: `status`, `output`, `notes`, `check` (`passed` or `failed`, with reasons), `used` (requests, tokens, cost, per provider), `events`, `options`, `refusal`, `writes`.

1. The skill must be ready. Its needs are bound to the folders of this run.
2. The entry runs. Each model call is counted, then sent with `x-muse-task` and `x-muse-privacy` and the token of the managed app `skills` (priority `normal`, never paid; `skills-paid` only when the run's budget has `paid_usd` above 0).
3. The skill's own check runs on what it returned.
4. **A failed check is retried once**, with every chat step one tier stronger: `light` to `everyday` to `heavy`. A step already on `heavy` stays there; if nothing can be raised there is no retry.
5. The result:

| Status | Exit code | When |
|---|---|---|
| `done` | 0 | The check passed |
| `partial` | 3 | The check passed, but something could not be done; the notes say what. Also when the budget or the router stopped a second try and a first answer exists. |
| `needs_choice` | 4 | The check failed twice, or the router held back a step the skill could not do without |
| `failed` | 1 | Not approved, a missing input, a privacy refusal, an error |

`needs_choice` carries **exactly what the router offered**, parsed from the `options=` trailer of its refusal: wait until a time, hand off to your own subscription app, pay about an amount. The list may be empty, and the runner never adds a choice of its own. After two failed checks, where there was no refusal, the only choice that can appear is the hand-off, and only if the router suggested it on an answer (`x-muse-handoff: suggest`). An answer that failed its check is still returned, marked `check: failed`, so an app can show it as a draft.

The client never sleeps or retries on a 429. The runner may sit out a short `Retry-After` when called with `wait_up_to=<seconds>` (default 0: never); the command line does not.

**Writes.** A skill returns proposed writes as data. They are applied when `run(..., apply_writes=True)` or a later `muse.skills.apply_writes(result)` is called, only when the check passed, and only as new files: an existing file is never overwritten. `muse skills run` asks on a terminal, writes with `--yes`, and otherwise lists what was proposed.

## The command line

```
muse skills list [--json]
muse skills show <name> [--json]
muse skills approve <name> [--yes]
muse skills run <name> [--input KEY=VALUE ...] [--folder PATH ...] [--out PATH]
                       [--task light|everyday|heavy|frontier] [--privacy public|standard|private|local]
                       [--pay USD] [--yes] [--json]
```

`--input KEY=-` reads the value from standard input, which keeps it out of shell history. `--task` sets the tier of steps that name none. `--pay` gives this one run a money budget; without it a run never pays. The answer goes to standard output; one line saying who answered and what it cost ("Answered by groq. Nothing spent."), the notes and any choices go to standard error. The first time a skill is used, its description and what it asks for are shown first.

## The shipped skill: answer over my files

```bash
muse skills run files-answer --folder ~/Documents/notes --input question="第三季的營收是多少？"
```

It answers from the files in the folders you name and cites the file and paragraph behind every statement. It is built for small free models: finding the passages happens on your computer, and the model reads only a few of them.

**Index.** Text, Markdown, reStructuredText, CSV, TSV, JSON, JSON Lines and HTML (tags, scripts and styles removed). UTF-8 first, then Big5 for older Traditional Chinese files. PDFs are read only when `pypdf` or `PyPDF2` is already installed; Muse adds no dependency for them, and otherwise says how many were skipped. Hidden files and folders are skipped; links that lead outside the folder are not followed. Text is split into paragraphs and packed into chunks of about 320 tokens; each chunk after the first repeats the tail of the previous one (at most about 80 tokens), and a table's header row rides with every chunk. The index is `MUSE_HOME/skills-data/files-answer/index.sqlite` (owner-only): path, size, modification time, chunk text, optional embedding. It holds text from your files in the clear, on this computer. A file whose size and modification time are unchanged is not read again; a deleted file leaves the index. Caps: 2,000 files, 2 MB a file, 50 MB read in one run, 20,000 chunks. Hitting one makes the result `partial` and the notes say which.

**Retrieve.** BM25 over the chunks of this run's folders, always, on your computer. If the router's `embed` alias answers and the budget has room, the question and the best 64 lexical candidates (every chunk when there are 64 or fewer) are embedded, ranked by cosine, and merged with the BM25 order by reciprocal rank fusion. Embeddings are kept in the index, so a later question costs one embedding call. If the router defers, refuses or has no `embed` alias, the skill carries on with words only. Only the top passages (default 6, `top_k`) go to the model.

**Answer.** One call at the job's tier (`everyday`) with the numbered passages, an instruction to answer only from them, to cite like `[2]`, to answer in the language of the question, and to return `{"answer", "claims": [{"text", "cites": [n]}], "not_found"}`. The reply is read defensively: code fences and surrounding prose are ignored, a cut-off object is repaired by closing what was open, and plain text with `[n]` markers is accepted. When no passage shares a word with the question, no model is asked.

**Check.** Every claim must cite at least one passage number that was given, and must share terms with what it cites. Terms are lower-cased words with a trailing plural `s` removed and common English stop words dropped, whole numbers such as `4.2`, and, for Chinese, Japanese and Korean, every overlapping pair of characters (bigrams), which needs no word segmentation. A claim passes when at least 30% of its terms appear in its cited passages taken together, at least 15% in each one, and at least 2 terms are shared (1 for a claim with fewer than 4 terms). A "not found" answer must carry no claims. A failed check names the claims by number.

**Output.** `text` (the answer with a source list such as `[1] lease.md › paragraph 3`), `answer`, `claims` with their sources, `citations`, `not_found`, the `passages` that were sent (for an app to show), and how retrieval ran.

**Limits.** The check is lexical: it catches a missing or wrong citation, not a subtle misreading of the right passage, and it fails an answer written in a different language from the passage it cites. Semantic search only re-ranks lexical candidates in folders with more than 64 chunks, so a passage that shares no word with the question is not found there. Paragraph numbers count blank-line-separated blocks (rows for tables), which may differ from what an editor shows. Quality on real small models is not yet measured; that is work package A9.
