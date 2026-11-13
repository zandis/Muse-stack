# zh-TW evaluation set

200 short tasks in Traditional Chinese as it is written in Taiwan, used by
`scripts/eval_models.py` to rank the models a Muse proxy can reach. Work package
A10 in `docs/spec/04-execution-plan.md`. How to run it: `docs/evals.md`.

**This set was written by an AI model (Claude), not by a person from Taiwan. No
native Taiwan reader has reviewed it yet. Until one has, do not trust its results
beyond a rough ordering.** A reviewer should check, item by item, that the
wording is natural Taiwan usage, that each expected answer is the only
reasonable one, and that the accepted alternatives in `expect` cover the ways a
Taiwan writer would actually phrase the answer. Items a reviewer rejects should
be fixed or deleted, not kept with a note.

## What is in it

| File | Items |
|---|---|
| `items-light.jsonl` | 90 light-tier items |
| `items-everyday.jsonl` | 110 everyday-tier items |
| `gold.jsonl` | one correct answer per item; the tests replay it to prove every item can be passed by its own scorer |

One JSON object per line:

| Key | Meaning |
|---|---|
| `id` | unique, stable |
| `lang` | `zh-TW` here, `en` in `evals/en-control/` |
| `tier` | `light` or `everyday`, the task tiers of `config/routing.yaml` |
| `skill` | which of the four first skills the item stands for: `setup`, `files`, `research`, `chores` |
| `task` | category, below |
| `prompt` | the user message (or `messages`, a list of `{role, content}`) |
| `expect` | the scoring rules; an item passes only when all of them pass |
| `notes` | what the item is about, for a reviewer |
| `non_tw_text` | `true` when the prompt holds Simplified Chinese or another language on purpose |

| Tier | Task | Items | What a pass needs |
|---|---|---|---|
| light | `classify` | 18 | the one right label out of five |
| light | `extract` | 18 | a JSON object with the right fields: ISO dates (a 民國 year converted), integer amounts, names |
| light | `yesno` | 10 | `{"answer": "是"/"否", "reason": ...}`; only `answer` is scored |
| light | `pick` | 10 | the letter of the matching option |
| light | `lang` | 12 | `zh-TW`, `zh-CN`, `en` or `ja`; half the items are Simplified against Traditional |
| light | `s2t` | 10 | a Simplified sentence rewritten in Traditional with Taiwan wording (軟體, not 軟件) |
| light | `normalise` | 12 | a 民國 date as `YYYY-MM-DD`, a phone number as `+886…`, an address split into fields |
| everyday | `cite_qa` | 26 | an answer from two to three numbered passages, citing exactly the right passage numbers |
| everyday | `not_found` | 12 | `not_found: true` and no source cited when the passages do not hold the answer |
| everyday | `summarise` | 16 | exactly N bullets, the key points present, within a length bound |
| everyday | `rewrite` | 14 | a polite rewrite (required and forbidden words) or a shortening (length bound, facts kept) |
| everyday | `arithmetic` | 16 | one number, time or date worked out from a short word problem |
| everyday | `format` | 14 | an instruction with a format constraint: JSON keys, bullet count, line pattern, length |
| everyday | `mixed` | 12 | the same kinds of task in mixed Chinese and English, Taiwan office style |

By skill: chores 90, files 58, research 32, setup 20.

## Scoring

Rules only; no judge model. The kinds an `expect` can hold:

| Kind | Passes when |
|---|---|
| `exact` | the whole answer equals the expected string, after removing a code fence, a label such as 答案：, quotes, trailing punctuation, case and character width |
| `one_of` | the same, against a list |
| `json_fields` | the first JSON object in the answer (bare, fenced or inside prose) has each field right: string, number (`1,200 元` reads as 1200), ISO date (`2025/3/14` is accepted; a 民國 year is not), boolean, list |
| `json_keys` | that object has exactly these keys |
| `contains_all` | every entry is in the answer; an entry is a string, a list of alternatives, or `{"re": pattern}` |
| `contains_any` | at least one entry is |
| `contains_none` | no entry is |
| `cites` | the cited passage numbers (the `sources` field of a JSON answer, else `[n]` marks) equal the expected set, or include it with `mode: superset` |
| `number` | the answer's number is within the tolerance; the number after 答案 wins, else the last number in the text |
| `max_chars` | the answer is at most this many characters |
| `bullets` | the number of bullet lines is right, and optionally each is short enough |
| `script` | `hard`: the answer must hold no Simplified character and no mainland term |

An empty answer always fails. Every item has at least one check that junk
cannot satisfy.

**Script sub-score.** For every zh-TW item the harness also records whether the
answer was in Traditional characters with Taiwan wording. It uses a small
built-in table (about 690 Simplified characters and 45 mainland terms), not
OpenCC. Unless the item says `script: hard` (the ten `s2t` items do), an answer
in Simplified is folded to Traditional before the content checks, so a right
answer in the wrong script keeps its pass and loses the script point. Read the
two numbers together. The table is small: it misses rarer characters, and it
cannot tell 發 from 髮.

## What it does not measure

- **Writing quality.** A summary passes if it has the right number of bullets
  and the key facts. Whether it reads well is not scored. A polite rewrite
  passes on 您 and the absence of the rude words, not on tone.
- **Long documents.** Every prompt is under about 400 characters.
- **The heavy tier,** multi-step work, tool use, or retrieval itself.
- **Safety,** refusals, or privacy.
- **Knowledge.** Items carry what they need; nothing depends on facts about
  the world, and there are no medical items.
- **Spoken or regional usage** beyond standard written Taiwan Mandarin. There
  is no Taiwanese Hokkien, Hakka or Indigenous-language content.

A pass shows that a model followed a short instruction in Traditional Chinese
closely enough for a program to use the result. A fail can be a wrong answer or
a right answer in a shape the rules do not accept; with `--keep-outputs` you can
look. Rule-based scoring is strict about format on purpose, because the skills
feed these answers to code.

Differences of a few points mean nothing at this size: with 90 light items one
item is 1.1 points, and a 95% interval around 80% is roughly ±8 points.

## How the items were written

All text is original, written for this set in November 2026. No passage is
copied from a publication. People, shops, services and figures are invented;
names are generic (陳小姐, 林經理, 阿哲, Kevin, Amy). Place names and roads are
real public ones with arbitrary numbers. Phone numbers are placeholder patterns.
Each item was written together with its scoring rule and one correct answer, and
the tests check that the correct answer passes and that empty or junk output
does not.

`evals/en-control/` holds 20 English items parallel to items here (each note
names its counterpart), so a model's zh-TW result can be read against its
English one. Twenty items give a rough reference, not a measurement.

## Licence

The same licence as the rest of this repository. When this set was written the
repository had no `LICENSE` file; until it has one, the set is not licensed for
reuse outside it.
