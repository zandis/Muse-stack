# Chores check: task sets

Tasks for `scripts/chores_check.py`, the harness behind gate G1 (since 2026-11-13: "at least 80% of completed runs are right by the expected facts, on free allowances, with nothing spent"; the skills' own check is reported second). How to run it and how to read the numbers: [docs/chores-check.md](../../docs/chores-check.md).

```
evals/chores/
  fixtures/larkspur-en/     seven English documents
  fixtures/qingfeng-zh/     seven Traditional Chinese documents
  files-answer/tasks.jsonl
  research-report/tasks.jsonl
```

Every document under `fixtures/` was written for this test. The people, addresses, companies, products, phone numbers and email addresses are invented, and each document says so. The two folders are different households, not translations of each other. Each has a lease, meeting notes, a product FAQ, invoices (CSV), a travel plan, a short policy and a longer handbook.

## A task

One JSON object per line in `<skill>/tasks.jsonl`. The folder name is the skill's name.

```json
{"id": "fa-014", "skill": "files-answer", "kind": "numeric", "lang": "en", "folders": ["larkspur-en"],
 "inputs": {"question": "How much is the monthly rent for Flat 4B?"},
 "expect": {"facts": [{"number": 1450, "source": "lease.md"}]}}
```

| Key | Meaning |
|---|---|
| `id` | Unique in the file |
| `skill` | The skill to run; must equal the folder name |
| `kind` | `lookup`, `multi-hop` (two files), `numeric`, `date`, `not-found`, `cross-lang` (question in one language, file in the other), `long-file`. Reports are broken down by it. |
| `lang` | The language of the question: `en` or `zh` |
| `folders` | Folders under `fixtures/` given to the run (empty for a skill that reads no folder) |
| `inputs` | Passed to the skill as they are |
| `expect` | The independent score, below |

`expect` is either `{"not_found": true}` (the right answer is that the files do not say; the skill's output must have `not_found: true`) or `{"facts": [...]}`. Every fact must be found in the answer text. A fact is one of:

- `{"any": ["31 August 2033", "August 31, 2033"]}`: one of these strings, ignoring case, spaces and thousands commas;
- `{"number": 1450}`: this number, however it is written (`1,450`, `1450.00`);
- `{"regex": "..."}`: a pattern, case-insensitive;
- typed values, strict on the value and lenient on the form: `{"date": "2031-04-03"}` (also `"year_optional": true`), `{"time": "08:40"}`, `{"amount": 1450}`, `{"count": 3, "unit": "year"}` (units: year, month, week, day, hour, minute, time, person). The forms each one takes are listed in docs/chores-check.md.

A fact may add `"source": "lease.md"` (or a list of acceptable files): the answer must cite that file. For a skill whose output has no `citations` or `sources` list, sources are not scored.

The harness refuses to start if a line does not parse, or if an expected fact is not in the file it names, so every task can be answered from its fixtures. Check without sending anything:

```bash
python scripts/chores_check.py --check-tasks
```

## Skills without tasks yet

`setup-services` and `recurring-chore` are being built separately and have no task file here. When one is installed, add `evals/chores/<skill-name>/tasks.jsonl` in the shape above: `inputs` as that skill takes them, `folders` only if it reads folders, and `expect.facts` over the text it returns (its output's `text`, `answer` or `report`). The harness finds task files by folder, runs the skills that are installed, and names the ones that are not as skipped. A task file for a skill that cannot be run here is not written in advance.
