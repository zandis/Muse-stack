# Ranking models on Traditional Chinese (Taiwan)

Work package A10. `scripts/eval_models.py` sends a small zh-TW test set through
a running Wardmuse proxy and reports, per routing alias or per model, how many items
each one gets right. The aim is to choose the `light` and `everyday` defaults in
`config/routing.yaml` for Taiwan users from data.

The set is in `evals/zh-tw/` (200 items) with an English control in
`evals/en-control/` (20 items). **The set was written by an AI model and has not
yet been reviewed by a native Taiwan reader; see `evals/zh-tw/README.md` for
that and for what a pass does and does not show.** Scoring is by fixed rules. No
judge model is used.

## Before you run it

1. `muse up`, with your provider keys set.
2. Recommended: give the evaluation its own background app, so it steps aside
   for anything you are doing and cannot spend money.

   ```bash
   muse apps add eval --priority background     # prints a key once
   export MUSE_EVAL_APP_TOKEN=sk-muse-app-...
   ```

   Without `MUSE_EVAL_APP_TOKEN` the script uses the master key, found the same
   way `scripts/real_key_check.py` finds it (environment, `.env`, key store).
   No key is ever printed or written to the report.

## Run it

A cheap sample first: 30 items on each chat alias, at most 120 requests with the
shipped catalogue's four chat aliases (`fast`, `cheap`, `smart`, `long-context`).

```bash
python scripts/eval_models.py --sample 30 --seed 1
```

The full run on the aliases: 220 items each, at most 880 requests.

```bash
python scripts/eval_models.py --suggest
```

Each model on its own, to rank them one by one (one target per pinned chat
deployment the proxy lists, so the count grows with the keys you have set):

```bash
python scripts/eval_models.py --deployments --suggest
```

If free tiers ran out part-way, continue later with the same options plus
`--resume`. Only items not yet answered are sent.

Before anything is sent the script prints the plan and asks to confirm (`--yes`
skips the question):

- each target, whether it is `free`, `trial` (a trial allowance that may not
  renew) or `paid`, and the providers it can reach;
- the worst-case number of requests (targets × items) and an upper bound on
  tokens (two per input character plus the output cap);
- which targets were left out.

Useful options:

| Option | Effect |
|---|---|
| `--only fast,cheap` | only these targets; a deployment id works too |
| `--only auto` | send model `auto` with `x-muse-task: light` or `everyday` per item, to test the routing rules as an app would use them |
| `--tier light`, `--tasks cite_qa,not_found`, `--no-control` | fewer items |
| `--sample K --seed S`, `--limit N` | a repeatable random K spread over the sets and tiers; the first N |
| `--max-wait 120`, `--stop-after 5`, `--delay 1` | see "Being a good citizen" |
| `--keep-outputs` | store the model's answers in the report |
| `--report PATH` | default `$MUSE_HOME/eval-report.json` |
| `--replay FILE` | score recorded outputs with no network (below) |

## Free and paid

The script works out what each target can reach from `config/providers.yaml`
and the keys that are set, the same rendering the proxy starts from.

- A target that can reach a paid or metered provider is **left out** unless you
  pass `--allow-paid`. With it, those targets (and only those) are sent with
  `x-muse-allow-paid: true`, and the daily paid cap still applies.
- With the master key, an alias counts as paid as soon as one paid provider has
  joined it (any paid key with the paid cap above zero, or a metered provider
  such as SiliconFlow). With the token of an app that may not pay, the proxy
  removes paid providers before routing, so the alias is judged by its free
  members and stays in. That is the second reason to use the `eval` app.
- A target the local catalogue does not know is treated as paid.

## Being a good citizen

- One request at a time, one second apart, `temperature: 0`, a small
  `max_tokens` per kind of task (160 to 300 for light work, 400 to 600 for
  everyday work). A deployment's output floor for reasoning models still applies.
- Named aliases and deployments get no `x-muse-task` header, so the routing
  rules do not replace the model being measured. Every request is marked
  `x-muse-privacy: public`: the prompts are public test text.
- On HTTP 429 the script honours `Retry-After` up to `--max-wait` seconds and
  tries once more. A longer wait, a second 429, or `x-muse-ration: refuse:…`
  marks the item `deferred` and moves on. After `--stop-after` deferrals in a
  row the target is stopped; `--resume` picks it up later.

## Reading the result

The table has one row per target: zh-TW pass rate overall and by tier, the
English control, the script sub-score (answers in Traditional characters with
Taiwan wording), answers that came back empty because a reasoning model used
its whole allowance thinking (`x-muse-answer: empty-reasoning`), deferred items,
errors, mean latency and cost from `x-muse-cost-usd`.

The JSON report adds pass rates by task and by skill, `zh_tw_gap` (English rate
minus zh-TW rate), and, for an alias, which deployment answered each item
(`answered_by`). An alias row measures the mix the router chose during the run,
not one model. A rate counts only items that were answered; deferred items and
errors are listed beside it.

By default the report holds item ids, checks and scores, not the answers.
Prompts are never stored; they are in the item files.

`--suggest` prints the best free target per tier and the lines one might change
in `config/routing.yaml`. It prints only and never edits the file. It will not
suggest a change from fewer than 20 answered items in a tier. Check an edit with
`muse rules check`.

How far to trust it: one light item is 1.1 points, and a 95% interval around 80%
on 90 items is about ±8 points. Free-tier models and limits also change from
week to week, so date the report you act on.

## Without a proxy

```bash
python scripts/eval_models.py --replay evals/zh-tw/gold.jsonl
```

`--replay` scores a file of recorded outputs: JSON lines of
`{"id": ..., "output": ..., "target": optional name}`, or a report written with
`--keep-outputs`. Use it to try a change to an item or to the scorer, or to
score answers collected some other way.

## Changing the set

Add a line to an `items-*.jsonl` file and a correct answer to `gold.jsonl`, then
run `python -m pytest tests/test_eval_models.py`. The tests check that every
line parses, ids are unique, each `expect` uses known kinds, the gold answer
passes, junk does not, and no zh-TW prompt contains a Simplified character or
mainland term unless the item sets `non_tw_text`.
