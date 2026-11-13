# Interface: the permission token (schema 1)

Status: draft for agreement by tracks A, D and E (work package C3). Implemented by
`core/src/permissions.rs` (Rust) and `muse/permissions/` (Python); both are held to
`core/conformance/permissions.json`. "Permission" is the word on screen; the code and
the specification call it a mandate (`docs/spec/01-architecture.md` §2.7).

Nothing here has been reviewed by an outside party. The plan books that review at gate G3.

## 1. What was checked before designing

| Fact | Source, checked 2026-11-13 |
|---|---|
| `biscuit-auth` 6.0.0 is the latest stable Rust release, published 2025-07-16. 7.0.0-alpha.1 was published 2026-11-11 and is not used. (The ecosystem review recorded 6.0.0 as released on 2026-11-11; that date belongs to the 7.0 alpha.) | crates.io API for `biscuit-auth` |
| `biscuit-python` 0.4.0 (2025-09-26) is the latest Python binding. Its `Cargo.lock` pins `biscuit-auth` 6.0.0, so both sides run the same Datalog engine and the same printer. | PyPI JSON for `biscuit-python`; the sdist's `Cargo.toml` and `Cargo.lock` |
| Wheels for 0.4.0 exist for CPython 3.12 and 3.13 on Linux x86_64 and aarch64 (manylinux 2.17 and musllinux 1.2), macOS x86_64 and arm64, and Windows amd64. There is no Windows arm64 wheel; an sdist exists (needs a Rust toolchain). | PyPI file list |
| Datalog block versions 3 to 6 (language 3.0 to 3.3). `check all` needs 3.1. Dates are UTC seconds, written as RFC 3339. Integer arithmetic is overflow-checked; an overflow fails the check. `+` joins strings; `.starts_with()` tests a prefix. | https://doc.biscuitsec.org/reference/specifications |
| Facts in a later block are visible only to that block. Rules, checks and policies of the authority block and of the authorizer see authority and authorizer facts only. A later block therefore cannot add authority. | same page, "scopes" |
| Each block has one revocation identifier: its signature. | same page |
| Every block is signed with a fresh random key pair, so two tokens with the same content have different bytes. Vectors cannot compare token bytes. | same page; `Biscuit::append` in the crate source |
| The Python binding builds, appends blocks, authorizes, queries, exposes revocation identifiers, block source and third-party blocks. It does **not** expose `seal()`. | `src/lib.rs` in the sdist; run locally |
| The printer does not escape strings: `purpose("a"b")` is printed for the value `a"b`. | run locally on both sides |

Consequences taken:

- Pin `biscuit-auth = "=6.0.0"` and `biscuit-python==0.4.0`. A new release of either is a
  deliberate change that reruns the vectors.
- No text that reaches a token may contain `"`, `\` or a control character (section 3.1),
  because the block text is what both implementations read back.
- Sealing is not part of schema 1 (not available in Python). A sealed token still verifies.
- Nothing in Python does cryptography by hand: signing and verifying, of tokens and of
  revocation entries, is done by the Biscuit library on both sides.

## 2. Keys and encodings

| Thing | Form |
|---|---|
| Root public key (`issuer`) | `ed25519/` + 64 lower-case hex digits (Biscuit's own text form) |
| Root private key | `ed25519-private/` + 64 hex digits. On the home: the single line of `MUSE_HOME/root.key`, mode 0600, until the vault package takes over (loading sits behind `KeySource` in Python). |
| Token | Biscuit, URL-safe base64 as the library writes it |
| Revocation identifier | 128 lower-case hex digits (an Ed25519 signature) |
| Time | Whole seconds since 1970 UTC, 0 to 253402300799. In the token: a Datalog date. |
| Amount | A non-negative 64-bit integer in minor units, with the number of decimals carried beside it |

Only Ed25519 is accepted in schema 1.

## 3. The authority block

The authority block holds facts only, in exactly this order, one per line. Lists are sorted
by code point and hold no duplicates. A token whose authority block is not byte for byte
what the implementation would print for the facts it reads is refused (`invalid_mandate`).

```
schema("muse.permission", 1);
mandate("m1_<64 hex>");
issuer("ed25519/<64 hex>");
holder("helper:vm");
issued_at(2026-11-13T00:00:00Z);
valid_from(2026-11-13T00:00:00Z);
valid_until(2026-11-30T00:00:00Z);
period_zone("Asia/Taipei");
privacy_max(1);
purpose("Pay for three research services", "");
confirm("over_amount", "TWD", 2, 5000);
action("pay.spend");              one line per action
routine("mail.draft");            zero or more; each must also be an action
resource("host", "api.example.com");     one line per resource
limit("spend", "TWD", 2, "day", 2000);   zero or more
```

### 3.1 Fields

| Field | Rule |
|---|---|
| `mandate` | `m1_` + SHA-256 (hex) of the UTF-8 text of the authority block **without its `mandate` line**. That text is the canonical form; no second serialisation exists. The verifier recomputes it (`bad_mandate_id` cannot occur silently: a mismatch is `invalid_mandate`). `issued_at` is part of the hash, so granting the same thing twice gives two ids. |
| `issuer` | Must equal the root key the verifier was given (`wrong_issuer`). |
| `holder` | `<kind>:<id>`, kind one of `device`, `helper`, `app`, `clinician`; id is an identifier. The enforcing module authenticates whoever presents the token and supplies that id; the token is otherwise a bearer token. |
| `action` | 1 to 32 dotted lower-case names (`files.read`). Class by registry: **read** `files.read`, `mail.read`, `calendar.read`, `health.read`, `records.read`, `llm.call`; **spend** `pay.spend`, `llm.paid`; every other name, known or not, is **write**. |
| `routine` | Actions the person marked "do not ask each time". |
| `resource` | `(kind, value)`, 1 to 64. Kinds: `folder`, `host`, `calendar`, `record_kind`, `provider`, `home`. The value `*` means any resource of that kind and must be written out; there is no implicit "any" and no "any kind". |
| `limit` | `(kind, unit, decimals, period, amount)`. Kinds: `spend` (unit is a currency code such as `TWD` or `USDC`), `requests`, `messages` (unit `""`, decimals 0). Period `day` or `month`. One limit per (kind, unit, period); one decimals value per unit. |
| `period_zone` | An IANA zone name. A day is a calendar day there, a month a calendar month there. The token only names the zone; the ledger that counts usage applies it (section 6). |
| `purpose` | Free text (at most 500 characters) and an optional standard code (identifier, or `""`). |
| `valid_from`, `valid_until` | Both required; `valid_from < valid_until`. There are no permissions without an end. |
| `confirm` | `("each_write", "", 0, 0)`, `("never", "", 0, 0)` or `("over_amount", unit, decimals, amount)`. `never` is valid only when every action is read-class or routine. `over_amount` needs a spend limit in the same unit and decimals. |
| `privacy_max` | Rank of the least protective class the holder may use for anything it sends out: `local` 0 < `private` 1 < `standard` 2 < `public` 3. A request names the class it will be handled under; it must rank at or below this. `local` means nothing leaves the home. |

Text rules, applied to every string before it enters a token and to every request:

- **Safe text**: no `"`, no `\`, no control character (Unicode category Cc). Purposes use
  「」 or '' instead of double quotes.
- **Identifier**: 1 to 128 of `A-Z a-z 0-9 . _ : @ + -`.
- **Folder**: starts with `/`, no trailing `/`, no empty, `.` or `..` segment, at most 1024
  bytes, safe text. `/` alone is not a folder; write `*`. Comparison is by code point. The
  enforcing module resolves links and normalises (NFC) before it asks; the token does not.
- **Host**: lower-case letters, digits, `.` and `-`, optionally `:port`. Exact match only.

### 3.2 Size

Measured with the fixtures: a typical authority block is 450 to 700 bytes; each narrowing
adds 150 to 350 bytes (two keys and a signature are about 130 of that). A token narrowed
three times stays under 2 kB, about 2.7 kB as base64.

## 4. Narrowing blocks

Anyone holding a token can append a block offline. A block of schema 1 holds **checks
only**, each at most once (limits and caps: once per key), in this order:

```
check if req_action("files.read") or req_action("mail.draft");
check if req_resource("folder", "/docs/work") or req_resource("folder", $v), $v.starts_with("/docs/work/") or req_resource("host", "a.example") or req_resource("calendar", $v);
check all amount("spend", "TWD", $a), used("spend", "TWD", "day", $n), $n + $a <= 500;
check all amount("spend", "TWD", $a), $a <= 12000;
check if time_late($t), $t >= 2026-11-14T00:00:00Z;
check if time($t), $t <= 2026-11-20T00:00:00Z;
check if req_privacy($p), $p <= 1;
check if confirm_applied("each_write");            or ("over_amount", "TWD", 2, 500)
check if req_job("job-7");
check if req_tool("call-19");
check if req_card("card-3");
```

| Narrowing | Meaning |
|---|---|
| Fewer actions | The request's action must be one of those listed. |
| Narrower resources | A folder is written as two alternatives: the folder itself, and anything under it **with the `/` appended**, so `/docs/work` covers `/docs/work/a.md` and not `/docs/workshop`. `req_resource("calendar", $v)` keeps "any calendar". |
| Lower limit | Compared with the same usage figures as the parent's limit (section 6). |
| Cap per request | A single amount may not exceed this. Used for "approve this payment of NT$120". |
| Later start, earlier end | |
| Stricter privacy | |
| Stricter confirmation | The verifier states which rules it applied (`confirm_applied`); a verifier that does not know the rule cannot satisfy the check. |
| One job | Not single-use: a job has many steps. |
| One tool call, one card | Single-use (section 7). The same block must carry an end time. |

Rules both implementations enforce when reading a token:

- A block that is not exactly the text printed from what was read (unknown check, fact, rule,
  different spacing), or that has an external (third-party) signature, makes the whole token
  `unknown_block`. Third-party blocks and re-delegation to another holder are not in schema 1.
- A limit or cap must name a (kind, unit) the authority block limits.
- `narrow()` additionally refuses a narrowing that is not inside the token it is applied to
  (`not_narrower`), so an honest holder cannot produce a copy that reads as wider. A token
  made by other software that lists, say, an action the parent lacks is still safe: every
  level is checked, so the extra action is never allowed, and `describe` shows the
  intersection.

Why widening is impossible: the authority facts are signed by the root key; a later block
cannot add facts the authorizer or earlier blocks can see (Biscuit scoping), and a check can
only fail a request, never pass one. The property tests append blocks holding facts, rules
and `check if true` and confirm that the answer does not change to allow.

## 5. Checking a request

```
check(token, request, usage, revocation_list, spent_set, now) ->
    allow | deny(reason) | needs_confirmation(rule)
```

A request: `holder`, `action`, `resource (kind, value)`, `amounts [(kind, unit, value)]`,
`privacy`, optional `job`, `tool`, `card`. The verifier applies these steps in order and
stops at the first that fails. Reasons are the machine-readable strings below; they are the
same in both languages and are fixed by the vectors.

| # | Step | Reason |
|---|---|---|
| 1 | The token decodes | `malformed_token` |
| 2 | None of its revocation identifiers is on the list (read without verifying, so a revoked token is refused before anything else) | `revoked` |
| 3 | The signature chain verifies under the root key | `bad_signature` |
| 4 | Schema, authority block and every later block are as sections 3 and 4 say | `unsupported_schema`, `invalid_mandate`, `wrong_issuer`, `unknown_block` |
| 5 | The request is well formed | `bad_request` |
| 6 | `request.holder` is the holder | `wrong_holder` |
| 7 | `now + skew >= start` and `now - skew <= end`, for the authority block and every block | `not_yet_valid`, `expired` |
| 8 | The action is listed at every level | `action_not_allowed` |
| 9 | The resource is covered at every level | `resource_not_allowed` |
| 10 | The privacy rank is at or below every level's | `privacy_not_allowed` |
| 11 | Job, tool and card bindings equal the request's | `wrong_job`, `wrong_tool`, `wrong_card` |
| 12 | A single-use token ends within 24 hours plus skew, and its tool or card id is not in the spent set | `single_use_too_long`, `already_used` |
| 13 | Amounts (section 6) | `amount_required`, `limit_missing`, `usage_missing`, `over_limit`, `over_cap` |
| 14 | Confirmation (below) | `needs_confirmation("each_write" \| "over_amount")` |
| 15 | The Biscuit authorizer, given the same request as facts, also allows | `engine_disagreement` |

Steps 5 to 14 are evaluated in code on the values read from the token; step 15 runs the
token's own Datalog against the authorizer in section 5.2. A request is allowed only when
both agree. Step 15 can never turn a refusal into an allow. `engine_disagreement` means a
bug; the property tests assert it never appears.

Clock skew: 60 seconds by default, at most 300. It widens both ends of every validity
window, including a five-minute approval.

### 5.1 Confirmation

A request is **approved** when the token is bound to a card and the request names that card
(step 11 has already required them to be equal). An approved request needs no further
confirmation. Otherwise every confirmation rule in the chain (authority and blocks) is applied:

| Rule | Asks when |
|---|---|
| `each_write` | The action is not read-class and not routine. |
| `over_amount(unit, N)` | The action is spend-class and any spend amount is in another unit or above N (routine or not); or the action is write-class and not routine (the standing rule: every write is confirmed). |
| `never` | Never. |

If any rule asks, the answer is `needs_confirmation`, with `each_write` taking precedence over
`over_amount`. The helper then shows a card; the approving device narrows the permission to that
card (action, resource, a cap equal to the amount, an end time minutes away) and the helper
presents the narrowed token with the card id (architecture §3.6).

### 5.2 The authorizer

Facts supplied by the verifier: `time(now - skew)`, `time_late(now + skew)`, `req_holder`,
`req_action`, `req_resource`, `req_privacy`, `req_job`, `req_tool`, `req_card` when present,
`amount(kind, unit, value)`, `used(kind, unit, period, value)`, and `confirm_applied(...)` for
each rule it applied. Policy, fixed text:

```
check if schema("muse.permission", 1);
check if holder($h), req_holder($h);
check if valid_from($d), time_late($t), $t >= $d;
check if valid_until($d), time($t), $t <= $d;
check if action($a), req_action($a);
check if req_resource($k, $v), resource($k, $v) or req_resource($k, $v), resource($k, "*") or req_resource("folder", $v), resource("folder", $p), $v.starts_with($p + "/");
check if privacy_max($m), req_privacy($p), $p <= $m;
check all amount($k, $u, $a), limit($k, $u, $d, $p, $max), used($k, $u, $p, $n), $n + $a <= $max;
allow if mandate($id);
```

Authorizer limits are set explicitly (10,000 facts, 1,000 iterations, 2 seconds); the library
default of 1 ms would turn a slow machine into a refusal.

## 6. Limits that need a count

A token cannot count. The verifier is given `usage`: for each limit, how much has been used
so far in the current period, keyed by (kind, unit, period). The check is
`used + amount <= limit`, with overflow treated as over the limit.

- **Who supplies usage: the enforcing module**, from the home's ledger, for the mandate id
  (the whole family: every narrowed copy draws on the same count). The token cannot detect a
  wrong figure. A module that reports zero defeats the limit; this is the same trust the
  module already holds, since it is the one that performs the action. The on-chain spending
  limit (architecture §2.12) is the independent backstop for money.
- A figure must be supplied for every limit that applies to an amount in the request,
  including limits added by narrowing; a missing figure is `usage_missing`, never zero.
- An amount whose (kind, unit) has no limit in the authority block is `limit_missing`:
  spending and counting are refused unless a limit says how much.
- `pay.spend` and `llm.paid` must carry a spend amount (`amount_required`).
- A lower limit added by narrowing is compared with the family's usage. It bounds the family
  total while that copy is used; it is not a separate budget. A separate budget per copy needs
  a ledger keyed by block and is left for later.
- The period boundary is the ledger's to compute, in `period_zone`.

## 7. Single-use tokens and the spent set

A block bound to a tool call or a card is good for one allowed request. The token cannot know
it was used; the verifier remembers.

- The **spent set** maps `<mandate id>/<tool|card>/<id>` to the token's end time plus skew.
  `check` reads it; `check_and_record` adds the id when the answer is allow.
- An entry is dropped once its time has passed (the token is expired by then anyway).
- The set is bounded (10,000 entries by default). When it is full of live entries the
  request is refused (`spent_set_full`): failing closed is the only safe answer.
- This is why a single-use token must end within 24 hours: the bound on memory is the bound
  on the token's life. Cards expire in 24 hours by default and payments in 5 minutes
  (`docs/spec/02-ux.md` §5).
- The set is local to one enforcing module. Two modules that both accept the same card id
  would each allow it once. In v1 one home enforces; say so again when a second enforcer exists.

## 8. Revocation

- A **revocation entry** is itself a one-block Biscuit signed by the root key:

  ```
  schema("muse.revocation", 1);
  revoked("<128 hex>", 2026-11-13T09:00:00Z);
  signer("ed25519/<64 hex>");
  ```

  An entry is valid when it verifies under the root key, has exactly that text and names that
  key as signer. Using a token as the envelope means no signature code of our own.
- The **list** is a set of entries: `{"v": 1, "entries": ["<base64>", ...]}`, sorted by
  identifier. It only grows.
- **Merge is set union.** Two entries for the same identifier keep the earlier time (then the
  smaller text), so merging is commutative, associative and idempotent, and any two devices
  converge.
- Revoking the root block's identifier revokes the whole family. Revoking a later block's
  identifier revokes that narrowing and everything narrowed from it, because every descendant
  contains that block.
- **Expiry is separate.** An expired token is refused by its dates and needs no entry; an
  entry is never removed when its token expires (the list stays append-only; pruning is a
  later, explicit operation).
- Only the root key signs entries in schema 1.

## 9. The sentence

`describe(token, lang)` returns one sentence and a list of lines, for `en` and `zh-TW`. It is
computed from the token alone, by the same code on every device, so the screen cannot show
something other than what the token allows. For a narrowed token it describes the effective
permission: the intersection of every level.

```
Your helper "vm" may spend money, limited to the service host api.example.com.
  Up to NT$20 a day
  Days and months are counted in Asia/Taipei time
  From 2026-11-13 00:00:00 UTC until 2026-11-30 00:00:00 UTC
  Asks you before any payment over NT$5, and before every other change
  May send data only to services rated private
  Purpose: Pay for three research services
```

The tests require that the holder, every action, every resource, every limit, the end date,
the confirmation rule, routine actions and the privacy class all appear in the output.
Times are shown in UTC; the app may add local time beside it but may not replace the text.

## 10. Threats and non-goals

| Threat | Answer in schema 1 |
|---|---|
| A narrowed token is stolen | It carries only what it was narrowed to and ends when it says. Revoke its block identifier, or the family. It is a bearer token: the enforcing module must authenticate the presenter as the holder. |
| A single-use approval is replayed | Spent set (section 7). |
| Clocks differ | 60 seconds of tolerance at both ends; never more than 300. |
| A holder narrows a token and gives it to someone else | The copy still names the original holder, so the other party must also be able to present as that holder. Handing authority to a different key (third-party blocks) is not in schema 1, and tokens that try are refused. |
| A block tries to widen | Impossible by construction (section 4); tested. |
| The enforcing module lies about usage | Not defended by the token (section 6). |
| The root key is stolen | Not defended here; the vault and recovery flow own that. |
| Text chosen to confuse the reader of a block | Quotes, backslashes and control characters are refused at issue and narrow. A block written by other software with such a string is either refused (`unknown_block`) or read as some valid narrowing; since a request needs both the reading and the real check to pass, the token never allows more than the sentence says. |
| Look-alike characters, un-normalised paths | Not defended; the enforcing module normalises before asking. |

## 11. What the compiler (C7) can derive

| Target | From |
|---|---|
| Router app entry | `holder`, `action` (`llm.call`, `llm.paid`), `limit("requests", ...)`, `limit("spend", ...)`, `privacy_max` |
| Skill tokens | `narrow()` to actions, resources, job; per call to tool; per approval to card |
| Spending limit on chain | `limit("spend", unit, decimals, period, amount)`, `valid_until`, `resource("host", ...)` |
| FHIR Consent | `holder`, `action`, `resource("record_kind", ...)`, `purpose` code, validity |
| Receipt | `mandate` id, the sentence |

## 12. Versioning

- `schema("muse.permission", 1)` is checked first. A change to any line format, to the
  registry of action classes, to the step order or to a reason string is schema 2.
- The Datalog used needs block version 4 at most (language 3.1, for `check all`).
- Changing the pinned Biscuit release requires regenerating nothing but rerunning the
  vectors on both sides; if the printer's output changes, that is a schema change.
