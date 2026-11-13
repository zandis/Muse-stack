# Record format, version 1

Status: implemented in Rust (`core/src/records.rs`) and Python (`muse/records/`), both held to the 3,050 cases in `core/conformance/records.json`. Not reviewed by anyone outside the project; no security audit has been done.

Version 1 is not frozen yet. It was amended once before any record left a development machine: the optional signed member `egress` (section 1.2) was added. A record without it has exactly the bytes, signature and hash it had before the amendment.

A **record** ("receipt" in the architecture, section 2.9) is one signed entry in a device's append-only chain: something was done in the person's name, by whom, under which permission, with what result and cost. This document says exactly which bytes are signed and what a verifier must check, so that two independent implementations agree and a third party can check an export with nothing but the file.

The identifier of this document inside export files is `record-format/1`.

Contents: 1 Record · 2 Canonical bytes · 3 Hashes, references and the signature · 4 Chain · 5 Verification · 6 Forks · 7 Checkpoints and proofs · 8 Export · 9 Privacy and redaction · 10 Worked example · 11 Decisions and reasons · 12 Limits · 13 Mapping to FHIR AuditEvent and ISO/IEC TS 27560 (informative)

---

## 1. Record

A record is a JSON object. Every member below is required unless marked optional; no other member is allowed.

| Member | Type | Meaning and allowed values |
|---|---|---|
| `v` | integer | Format version. Always `1`. |
| `device` | string | The signing device: its Ed25519 public key, 32 bytes as 64 lower-case hex characters. The device id *is* the key, so a record names what verifies it. |
| `seq` | integer | Position in this device's chain, from 0. |
| `time` | integer | When the action finished, in milliseconds since 1970-01-01T00:00:00Z, by the device's clock. |
| `mandate` | string or null | Id of the permission the action ran under; `null` for the person's own direct action. 1 to 128 characters of `A-Z a-z 0-9 . _ : -`. |
| `actor` | string | `person`, `helper` or `system`. |
| `action` | string | Dotted lower-case name, for example `files.write`, `llm.call`, `pay.spend`, `mandate.issue`, `mandate.revoke`, `card.answer`, `home.pause`. Two to four segments separated by `.`; each segment is 1 to 32 characters, starts with `a-z` and continues with `a-z 0-9 _`. The names in use are listed in section 1.3. |
| `resource` | string or null | A keyed reference to what was acted on (section 3.2): 64 lower-case hex. Never the content, a name, or a bare hash of either. `null` when the action has no resource. |
| `summary_hash` | string | Commitment to the summary (section 3.3): 64 lower-case hex. This is what the signature covers in place of the summary. |
| `outcome` | string | `ok`, `declined`, `failed` or `undone`. |
| `cost` | object or null | `{"currency": code, "minor": integer}` or `null`. `currency` is 3 to 8 characters of `A-Z 0-9`: an ISO 4217 code, or `USDC`. `minor` is a non-negative count of the currency's minor unit (ISO 4217 exponent; 6 decimals for `USDC`), so NT$30 is `{"currency":"TWD","minor":3000}`. |
| `links` | object, optional | Pointers to related things. Present only when non-empty. Allowed members, all strings: `job`, `card`, `payment_ref` (ids, same alphabet as `mandate`), `undo_of` (the hash of the record this one undoes, 64 hex). |
| `egress` | string, optional | Where the content of the action went (section 1.2): `none`, `home`, `service:<id>` or `host:<name>`. Signed when present. Absent means the record does not say. |
| `prev` | string | Hash of the previous record in this chain (section 3.4), or the genesis value for `seq` 0. 64 lower-case hex. |
| `sig` | string | Ed25519 signature (section 3.5): 64 bytes as 128 lower-case hex. |
| `summary` | string, optional | Short human text that is safe to show (section 1.1). Not signed directly; bound through `summary_hash`. |
| `summary_salt` | string, optional | The salt of the summary commitment: 16 bytes as 32 lower-case hex. Present exactly when `summary` is. |

All integers are in 0 to 2^53 − 1 (9007199254740991). There is no floating-point number anywhere in a record, a checkpoint or an export; `30.0` and `3e3` are malformed.

An **undo** is a new record with `outcome: "undone"` and `links.undo_of` naming the record it reverses. Records are never changed or removed.

### 1.1 The summary rule

The summary is the one free-text field, and it is stored and shown in clear, so it is restricted.

1. **Normalised.** The signer applies Unicode Normalization Form C (NFC) to the text before anything else; the normalised text is what is stored, committed to and signed. (NFC, not NFKC: full-width forms, ligatures and superscripts are kept as written.)
2. **Bounded and printable.** After normalisation: 1 to 200 Unicode scalar values; none of U+0000–U+001F, U+007F–U+009F, U+2028, U+2029, U+202A–U+202E, U+2066–U+2069 (control characters, line separators and bidirectional overrides, which could make the line read differently from what was signed).
3. **Not key-shaped** (checked when a record is made, not when it is verified). A *token* is a longest run of ASCII letters, digits and `_ + = -`. The summary is refused when it contains `-----BEGIN`; or a token of 20 or more characters that starts with one of `sk-` `sk_` `pk_` `rk_` `gsk_` `csk-` `nvapi-` `jina_` `hf_` `glpat-` `ghp_` `gho_` `ghu_` `ghs_` `ghr_` `github_pat_` `xoxa-` `xoxb-` `xoxp-` `xoxr-` `xoxs-` `AKIA` `ASIA` `AIza` `eyJ`; or a token with a piece (between hyphens) of 32 or more characters that holds both a letter and a digit (a hash, a base64 blob, most API keys).

A verifier takes the text as the bytes it is given (it does not normalise again) and checks part 2. Part 3 is a guard against accidents in the writer, not a guarantee: a secret written in words, or in pieces shorter than the thresholds, passes. The rule the writer must follow is "say what happened in words; never put content, keys or identifiers of people in a summary".

### 1.2 Where the content went (`egress`)

The Records screen has a filter "Left my devices". `egress` is the member it reads. It answers one question about one action: **where was the content this action worked on sent?** The *content* is the thing itself: the prompt and what was attached to it, the text of a file, the body of a message, an answer. Not the fact that something happened, and not the summary.

| Value | Meaning |
|---|---|
| `none` | The content went nowhere: the action happened on the device that signed the record and the content stayed there. A setting changed, a question was answered, a file was written on this device, a model on this same device answered. |
| `home` | The content was sent to the person's own home (another device of theirs), and no further. A phone that hands a file to the home writes this; the home never writes it about its own actions (for the home, that is `none`). |
| `host:<name>` | The content was given to a host bot: a program on the person's computer that Muse does not control, named as the home knows it (the name of its device or app, for example `host:openmausbot-tools`). Muse cannot say where the host sends it next, so this counts as having left. |
| `service:<id>` | The content was sent to an outside service. `<id>` is the service's id in Muse's catalogue (`service:gemini`, `service:nvidia_nim`), or, for a service the catalogue does not list, another stable lower-case id such as its host name. |

`<id>` and `<name>` are 1 to 64 characters of `a-z 0-9 . _ -`, starting with `a-z` or `0-9`. Nothing else is a valid value: no upper case, no spaces, no other prefix.

Rules for the writer:

- **One destination per record.** A record names the one place its action sent the content. Work that reached several places is several actions and several records (one model call, one record), so the filter and an export can show each destination on its own.
- **The farthest place counts.** Of `none`, `home`, `host:…` and `service:…`, the record carries the last one that applies to its action.
- **Say it only when you know.** A writer that cannot tell leaves `egress` out. Absent is not `none`.
- A value is the writer's signed statement, not a proof (section 12).

For a reader: the content **left the person's devices** exactly when `egress` starts with `service:` or `host:`. `none` and `home` did not leave. A record without `egress` is shown as "not stated" and is not counted either way; records written before the amendment are all of this kind.

`egress` is signed (section 2), so it cannot be changed, added or removed after signing without the signature failing. It is not hidden by redaction.

### 1.3 Action names in use

The format fixes the shape of an action name, not the list. These are the names written today; a package that adds an action adds its name here.

| Action | Written by | When |
|---|---|---|
| `home.pause`, `home.resume` | the home | The person paused or resumed (`muse pause`, `muse resume`, or the home API). |
| `card.create` | the home | A device asked the person a question (a card was made). `links.card` is the card. |
| `card.answer` | the home | A card was answered: `ok` for approved, `declined` for declined. `links.card` is the card. |
| `app.create`, `app.update`, `app.remove`, `app.rotate` | the home | An app was added, changed or removed, or its token was replaced, through the home API. |
| `key.store` | the home | A service's key was stored through the home API. The summary names the service's id only; never the key, any part of it, or its hash. |
| `job.queue`, `job.cancel` | the home | A background job was queued or cancelled through the home API. `links.job` is the job. |
| `skill.run` | the home | A skill run started through the home API finished. `egress` is `host:<name>` when a host bot asked for it and so received its answer. |
| `llm.call` | the home | A skill run used an outside service; one record per service, with `egress: service:<id>`. |
| `files.write` | the home | A file a skill proposed was written after its card was approved (`ok`), or could not be (`failed`). `resource` is `file:<path>`, `links.card` the card. |
| `mandate.issue`, `mandate.revoke`, `pay.spend` | reserved | For the permissions and payment packages. |
| anything else | a host bot, through `POST /v1/receipts` | A host's own report of what it did. Always `actor: helper` and `mandate: device:<name>`. A host may not use a name whose first segment is `home`, `card`, `app`, `key`, `job`, `skill`, `mandate` or `records`: those are the home's own. |

---

## 2. Canonical bytes

Records are signed and hashed as **RFC 8785 (JSON Canonicalization Scheme)** bytes, restricted to integers:

- UTF-8, no byte-order mark, no whitespace; `,` and `:` are the only separators.
- Object members are sorted by key, comparing keys as sequences of UTF-16 code units.
- Strings are written with `\"` and `\\`, with `\b \f \n \r \t` for those five characters, with `\u00xx` (lower-case hex) for every other character below U+0020, and with every other character as itself.
- Numbers are integers in ±(2^53 − 1), written in decimal with no sign for non-negative values, no leading zeros, no fraction, no exponent. In this range that is exactly RFC 8785's number form, so a general JCS library produces the same bytes; anything else is refused rather than rounded.
- `null`, `true`, `false` as written.

Text is not normalised by canonicalisation. The one place normalisation happens is the signer's step in section 1.1 (and section 3.2 for resources), before the bytes exist.

The **signed fields** of a record are `v`, `device`, `seq`, `time`, `mandate`, `actor`, `action`, `resource`, `summary_hash`, `outcome`, `cost`, `prev`, and `links` and `egress` when present. `summary`, `summary_salt` and `sig` are not among them. An absent optional member is left out of the object, never written as `null`, so a record without `links` or `egress` has the same bytes whether or not the writer knew those members existed.

- `canonical_bytes(record)` = JCS of the object holding the signed fields.
- `canonical_bytes(record, with_sig)` = JCS of the signed fields plus `sig`.

**Reading.** A verifier reads JSON strictly: UTF-8 without a byte-order mark; no repeated key in any object; no fractional or exponent number, no `-0`, no integer outside ±(2^53 − 1), no `NaN`/`Infinity`; no lone surrogate escape; nothing after the value. These are the places JSON parsers disagree, and a file that two verifiers could read differently is refused. Any JSON whitespace and key order is accepted on reading; only the canonical form is ever hashed.

---

## 3. Hashes, references and the signature

SHA-256 throughout. Every use has its own domain tag: an ASCII string ending in one zero byte, written here as `"…\0"`. `||` is concatenation.

| Value | Definition |
|---|---|
| Resource reference (3.2) | `HMAC-SHA-256(reference key, "muse/record/v1/resource\0" \|\| UTF-8(NFC(resource)))` |
| Summary salt (3.3) | first 16 bytes of `HMAC-SHA-256(reference key, "muse/record/v1/summary-salt\0" \|\| device hex \|\| 0x00 \|\| seq in decimal)` |
| Summary hash (3.3) | `SHA-256("muse/record/v1/summary\0" \|\| salt (16 bytes) \|\| UTF-8(summary))` |
| Genesis (3.4) | `SHA-256("muse/record/v1/genesis\0" \|\| device public key (32 bytes))` |
| Record hash (3.4) | `SHA-256("muse/record/v1/hash\0" \|\| canonical_bytes(record, with_sig))` |
| Signing digest (3.5) | `SHA-256("muse/record/v1/sig\0" \|\| canonical_bytes(record))` |
| Checkpoint digest (7.2) | `SHA-256("muse/checkpoint/v1/sig\0" \|\| JCS({device, size, root, time}))` |

### 3.2 Resource reference

The *resource string* is the writer's canonical name for the thing acted on, for example `file:/Notes/週報.md`, `payee:api.example.com`, `mandate:m-2026-11-001` (1 to 1,024 characters; the naming scheme per action belongs to the package that defines the action). The record stores only the HMAC above, under a **reference key** that is per vault and never leaves the person's devices.

The same resource always gives the same reference within one vault, so the person's own devices can answer "everything done to this file". Without the key a reference reveals nothing and cannot be tested against a guess; a bare hash of a file name or of a short lab result could be.

### 3.3 Summary commitment

The signature covers `summary_hash`, not the summary. The salt is per record and is derived from the reference key, so making a record needs no random numbers and is reproducible; to anyone without the reference key the salt is indistinguishable from random.

A record that carries `summary` and `summary_salt` is checked by recomputing the hash. A record with both removed (**redacted**) has exactly the same signed bytes, signature and record hash. Section 9 says what that hides.

### 3.4 Record hash and `prev`

The record hash covers the signed fields *and* the signature, so it names one signed record. `prev` of record `n` is the record hash of record `n − 1`; `prev` of record 0 is the genesis value, which ties the chain to its device from the first record. The Merkle tree in section 7 is built over the same record hashes.

### 3.5 Signature

`sig` = Ed25519 (RFC 8032, pure Ed25519) over the 32-byte signing digest, with the key whose public half is `device`. Ed25519 is deterministic: the same record and key always give the same signature, which is why the conformance vectors can pin signatures byte for byte.

Verification is RFC 8032 section 5.1.7 as OpenSSL and ed25519-dalek's `verify` implement it: `S` must be below the group order; the equation is checked without the cofactor, comparing `R` as encoded bytes; a public key of small order is not refused. The last point is deliberate and harmless here: a device id is a key the device chose, and a device that publishes a degenerate key only makes its own chain forgeable. Sixty-four vectors hold these edge cases so that the two implementations cannot drift apart on them.

---

## 4. Chain

One chain per device. Record `n` of device `D` has `device = D`, `seq = n`, `prev` as in 3.4, and a `time` not earlier than record `n − 1`'s by more than the **tolerance of 120,000 ms**. The tolerance exists because clocks get corrected and because two processes may read the clock in one order and append in the other; it is not a licence to backdate. Only neighbours are compared, so `append` (which sees only the head) and `verify` apply the same rule.

A device key is never rotated inside a chain: a new key is a new device with its own chain starting at `seq` 0. How the person's devices tell each other which device keys are theirs belongs to pairing (architecture 2.10), not to this format.

`append(head, entry)` is a pure function: given the previous record (or nothing) and an entry `{time, actor, action, outcome, summary, mandate?, resource?, cost?, links?, egress?}` it applies the summary rule, computes the resource reference, salt and summary hash, sets `seq` and `prev`, and signs. Storage must make "read the head, sign, store" one atomic step; signing two different records for one `seq` is a fork (section 6) and is the one thing a device must never do.

---

## 5. Verification

`verify_record(record, device?)` checks one record alone, in this order, and reports the first failure:

| Kind | Check |
|---|---|
| `malformed` | The shape and value ranges of section 1. |
| `wrong_device` | `record.device` equals the expected device, when the caller gave one. |
| `bad_signature` | `sig` verifies over the signing digest under `device`. |
| `summary_mismatch` | If `summary` is present, `summary_hash` equals the hash of `summary_salt` and `summary`. |

`verify_chain(records, device?)` walks records in the order given, expecting `seq` 0, 1, 2, … It returns the number of records, or the **index and kind of the first problem**. Per record: the four checks above (the device is taken from the first record when not given), then:

| Kind | Check |
|---|---|
| `missing` | `seq` is larger than the position: a record was removed, or the order was changed. |
| `duplicate` | `seq` is smaller than the position and the record is identical to the one already seen at that `seq`. |
| `fork` | `seq` is smaller than the position and the record differs from the one already seen at that `seq` (both validly signed). |
| `wrong_prev` | `prev` equals the hash of the record before it (or the genesis value at position 0). |
| `time_went_back` | `time` is not more than 120,000 ms before the previous record's. |

The signature is checked before sequence and linkage, so every kind after `bad_signature` is a statement about records the device really signed. What each kind of damage looks like:

| What was done to the chain | First problem |
|---|---|
| Any signed field edited (also `seq`, `prev`, `device`) | `bad_signature` at that record |
| Summary text or salt edited | `summary_mismatch` at that record |
| A record removed from the middle, or from the front | `missing` at the position of the gap |
| Two records swapped, or any reordering | `missing` at the first position out of place |
| A record repeated | `duplicate` at the second copy |
| A record replaced by another one the device signed | `wrong_prev` at the next record (or `fork`, if both are present) |
| A record signed by another key spliced in | `wrong_device` |
| Records removed from the **end** | Nothing: the shorter chain is valid. Only a checkpoint (section 7) or another device's copy shows it. |

---

## 6. Forks

A **fork** is two different records with the same `device` and `seq`, both with valid signatures. It cannot happen through honest use of one store; it means the key signed two histories (a restored backup that kept appending, two copies of a home running on one key, or a stolen key).

A fork is evidence in itself: the two records, verified individually, prove it to anyone. It shows up as:

- `fork` when a verifier is handed both records in one sequence;
- `wrong_prev` when it is handed one history up to some point and the other after it;
- two checkpoints of the same device that fail the consistency check of section 7.4.

The format does not say which branch is true; it makes the disagreement undeniable. A reader that finds one should show both records and stop treating that device's chain as complete.

---

## 7. Checkpoints and proofs ("sealed")

### 7.1 Merkle tree

The tree is that of RFC 6962 section 2.1 over the record hashes `h[0..n)`:

- leaf node = `SHA-256(0x00 || h[i])`
- inner node = `SHA-256(0x01 || left || right)`
- the root of zero records is `SHA-256("")`; the root of one is its leaf node; for `n > 1` the tree splits at `k`, the largest power of two smaller than `n`: `root = inner(root(h[0..k)), root(h[k..n)))`.

The `0x00`/`0x01` prefixes keep a leaf from ever being read as an inner node.

### 7.2 Checkpoint

```json
{"device": "…64 hex…", "size": 8, "root": "…64 hex…", "time": 1794000300000, "sig": "…128 hex…"}
```

`size` is how many records are sealed (records 0 to `size − 1`), `root` their Merkle root, `time` when the checkpoint was made (ms). `sig` is Ed25519 by the device key over the checkpoint digest of section 3. No other member is allowed. The digest's domain tag differs from the record's, so a record signature can never pass as a checkpoint signature or the reverse.

`verify_checkpoint` checks the shape (`malformed`), the device when one is expected (`wrong_device`) and the signature (`bad_signature`).

A checkpoint is what turns "the chain is internally consistent" into "the chain had exactly these `size` records at that time". On its own device it catches a tail cut back below the last seal. Its full value comes from keeping it somewhere the device does not control; anchoring (an attestation on the test network, a transparency log) is package E4 and is **not** defined here.

### 7.3 Inclusion proof

For record `m` in a tree of `n`, the proof is RFC 6962's audit path, a list of node hashes: empty for `n = 1`; otherwise, with `k` as above, `path(m, h[0..k)) + [root(h[k..n))]` if `m < k`, else `path(m − k, h[k..n)) + [root(h[0..k))]`.

Verification (RFC 9162 section 2.1.3.2), given the record hash, `m`, `n`, the path and the root: fail if `m ≥ n`. Set `fn = m`, `sn = n − 1`, `r` = the leaf node. For each `p` in the path: fail if `sn = 0`; if `fn` is odd or `fn = sn`, set `r = inner(p, r)` and, if `fn` is even, shift `fn` and `sn` right until `fn` is odd or zero; otherwise `r = inner(r, p)`; then shift both right once. Accept when `sn = 0` and `r` equals the root. A path longer than 64 is refused.

The path fixes the position but not always the size: for some positions the same path also fits `n + 1`. The size is fixed by the signed checkpoint, which is why proofs are only ever checked against a verified checkpoint.

### 7.4 Consistency proof

A consistency proof shows that the tree of the first `m` records is a prefix of the tree of `n ≥ m` records: nothing sealed earlier was changed or removed. It is RFC 6962's `PROOF(m, D[n])`: with `SUBPROOF(m, D[m], true) = []`, `SUBPROOF(m, D[m], false) = [root(D[m])]`, and for `m < n`: `SUBPROOF(m, D[0..k), b) + [root(D[k..n))]` if `m ≤ k`, else `SUBPROOF(m − k, D[k..n), false) + [root(D[0..k))]`.

Two cases need no proof and take an empty one: `m = n` (the roots must be equal) and `m = 0` (the old root must be the empty root). Otherwise verification is RFC 9162 section 2.1.4.2: if `m` is a power of two, put the old root at the front of the path; set `fn = m − 1`, `sn = n − 1` and shift both right while `fn` is odd; `fr = sr =` the first element; for each further element `c`: fail if `sn = 0`; if `fn` is odd or `fn = sn`, set `fr = inner(c, fr)`, `sr = inner(c, sr)` and, if `fn` is even, shift both right until `fn` is odd or zero; otherwise `sr = inner(sr, c)`; then shift both once. Accept when `fr` is the old root, `sr` the new root and `sn = 0`. A path longer than 128 is refused.

---

## 8. Export

An export is one JSON object, written as canonical JSON plus a final newline (any strict JSON is accepted on reading):

```json
{
  "type": "muse.records.export",
  "format": 1,
  "spec": "record-format/1",
  "device": "…64 hex: the device public key…",
  "checkpoint": { "device": "…", "size": 8, "root": "…", "time": 1794000300000, "sig": "…" },
  "complete": false,
  "records": [ { …record… }, { …record… } ],
  "proofs": [ ["…", "…", "…"], ["…", "…", "…"] ]
}
```

- **Complete** (`"complete": true`, no `proofs`): `records` holds records 0 to `size − 1`.
- **Subset** (`"complete": false`, with `proofs`): `records` holds any selection of the sealed records in ascending `seq`, and `proofs[i]` is the inclusion proof of `records[i]` at index `records[i].seq` in the tree of `checkpoint.size`. A filter that matches nothing gives an empty, still valid, export.
- In either form any record may be redacted (section 9).

`verify_export(file, device?)` needs nothing but the file. In order:

1. The shape of the whole file, checkpoint, every record and every proof: `malformed` (no index).
2. If the caller expects a particular device: `wrong_device`.
3. The checkpoint is by the file's `device` and its signature verifies: `bad_checkpoint`.
4. Complete: `verify_chain` over the records (its kinds, with the index); then `size_mismatch` unless there are exactly `size` records; then `root_mismatch` unless their Merkle root is the checkpoint's.
5. Subset, per record (with its index): `wrong_device`, `bad_signature`, `summary_mismatch`; `malformed` if its `seq` is not greater than the previous record's; `bad_proof` unless the inclusion proof leads to the checkpoint root; `wrong_prev` if it directly follows the previous record in `seq` and does not link to it.

It returns the device, the sealed size, the number of records in the file, whether it is complete, and how many records are redacted.

**What a successful check means.** The holder of the key `device` signed each of these records, and signed a statement that they are records number so-and-so of exactly `size`. It does not mean the key belongs to any particular person: the reader must get the device key another way (the owner reads it out, or shows `muse records verify`) and compare. It does not mean no later records exist, and for a subset it does not say what the other records are.

---

## 9. Privacy and redaction

What someone holding an export learns, and does not:

| They see | They do not see |
|---|---|
| Device public key | Who the person is (unless the summaries say) |
| For each record: `seq`, time to the millisecond, `actor`, `action`, `outcome`, `cost`, `mandate` id, `links` ids, and `egress` (which service or host bot the content went to) | The content acted on, file names, prompts, results |
| Summaries, as written, unless redacted | What a redacted summary said |
| Resource references: that two records touched the same thing | What the thing is; whether it is the same thing as in another person's vault |
| How many records are sealed (`size`), so how many they were not given | Anything about records left out of a subset beyond their count |

Two things remain visible by design and should be weighed before sharing a file: the **pattern** (when, how often, what kind of action, what it cost) and the **ids** in `mandate` and `links`. A subset export is the tool for the first; ids should be opaque.

**Redaction.** `redact(record)` removes `summary` and `summary_salt`. Nothing else changes: same signed bytes, same signature, same record hash, so the record still verifies, still links, and its inclusion proof still holds. This works because the signature covers `summary_hash` rather than the text (decision 3 in section 11).

Redaction hides the text because the commitment is salted with 128 bits that leave with the summary. Without the salt, a 200-character summary drawn from a small set of likely sentences ("Paused the queue") could be found by hashing guesses; with it, a redacted record cannot be tested against any guess, and two records with the same summary do not have the same `summary_hash`. The person's own devices, holding the reference key, can re-derive every salt and so can always show their own summaries again from their own copy.

Redaction is per record and can be done by anyone holding the record, not only the signer. It cannot be undone from the redacted record alone.

---

## 10. Worked example

All values below are produced by the conformance generator (`muse/records/vectors.py`) and checked by a test, so they stay true. **The keys are test-only** and public.

```
Ed25519 seed (test only)   000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
reference key (test only)  404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f
device (public key)        03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8
genesis                    9850c6a9ce0d6387a24cfcfc6d79ece5f6a9ef787205783babfb64e9a73d220e
```

Record 0 exists already (the person issued a permission); its record hash is

```
d672c7fb2fdc3c4fe5e59cbe33e0490b1008af0745cd1e0dfb2f909422b87800
```

The helper then writes a file under that permission. The entry handed to `append`:

```json
{"time": 1794000001500, "actor": "helper", "action": "files.write", "outcome": "ok",
 "mandate": "m-2026-11-001", "summary": "寫入「週報.md」（繁體中文摘要）",
 "resource": "file:/Notes/週報.md", "links": {"job": "job-17", "card": "card-3"}}
```

Step by step (`seq` is 1, `prev` is record 0's hash):

```
resource reference   30ed2657355fb0eefcf3fe9c611e249e58d23cca65d6cbf287ab1c25577202f4
summary salt         53457222fd679677d137b545020b1add
summary hash         3bf929d1bce9f1a9ce7013aaf236a19bf03ebedf3ffe4b1af03eb0f019351524
```

Canonical bytes of the signed fields (480 bytes, one line, shown wrapped):

```
{"action":"files.write","actor":"helper","cost":null,"device":"03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8","links":{"card":"card-3","job":"job-17"},"mandate":"m-2026-11-001","outcome":"ok","prev":"d672c7fb2fdc3c4fe5e59cbe33e0490b1008af0745cd1e0dfb2f909422b87800","resource":"30ed2657355fb0eefcf3fe9c611e249e58d23cca65d6cbf287ab1c25577202f4","seq":1,"summary_hash":"3bf929d1bce9f1a9ce7013aaf236a19bf03ebedf3ffe4b1af03eb0f019351524","time":1794000001500,"v":1}
```

```
signing digest       b592eb2ea0264872acd84ffd56b2d3a0c6c7fb8a39b1d39a3b0bafee3fb29131
sig                  6a149d47744c2c9e430a57304d7519a3504f06b6747894a6dc9687a077fc47206a1c51a0134e01602c5e248bdc3a2e757c127cae242150a3e1b01e0f8318be0c
record hash          f0919bd229d0f179919893200a1421c93de433379787fd96c527ab518568f5ed
```

The record as stored:

```json
{
  "v": 1,
  "device": "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",
  "seq": 1,
  "time": 1794000001500,
  "mandate": "m-2026-11-001",
  "actor": "helper",
  "action": "files.write",
  "resource": "30ed2657355fb0eefcf3fe9c611e249e58d23cca65d6cbf287ab1c25577202f4",
  "summary_hash": "3bf929d1bce9f1a9ce7013aaf236a19bf03ebedf3ffe4b1af03eb0f019351524",
  "outcome": "ok",
  "cost": null,
  "links": {"job": "job-17", "card": "card-3"},
  "prev": "d672c7fb2fdc3c4fe5e59cbe33e0490b1008af0745cd1e0dfb2f909422b87800",
  "summary": "寫入「週報.md」（繁體中文摘要）",
  "summary_salt": "53457222fd679677d137b545020b1add",
  "sig": "6a149d47744c2c9e430a57304d7519a3504f06b6747894a6dc9687a077fc47206a1c51a0134e01602c5e248bdc3a2e757c127cae242150a3e1b01e0f8318be0c"
}
```

Redacted, it is the same object without the `summary` and `summary_salt` lines; digest, signature and record hash are unchanged.

Six more records follow (the chain of eight is "chain A" in the vectors). The checkpoint over all eight:

```json
{
  "device": "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",
  "size": 8,
  "root": "584ca0a2336e11a43e5a3d1ec691970ced0feddef4d727e3b552b5e45cf37023",
  "time": 1794000300000,
  "sig": "c28301c81c6ee8ad7f6e376aead46f26ac915aae7c97e78bff5be0362a9cf89a9712e330e4803b46881d895b6037b6554d8c285e3083683667721a2f0cff740d"
}
```

The inclusion proof of record 1 in that tree (its leaf node is `7b678e8ebd5bb9ba5f15fd56cb82d734a327a4034e0492e030cbb33c94b97aa7`):

```json
["bf77976cb70799cbcee90b0da6e6194233be8ce7bee10c7f373b7091a8a8242f",
 "8dc85d538c3f828da1d6a25bc7858d540569ee5c3cb6c858c8ef232a12e56744",
 "3d2d81fae657df0377a72cccade1202881e48b71a981f463835ab89ecaa9a6c0"]
```

A subset export holding only record 1 is the envelope of section 8 with this checkpoint, this record and this proof.

---

## 11. Decisions and reasons

1. **Canonical JSON is RFC 8785, integers only.** A published standard with independent implementations, readable in any language, and with no floats there is no number-formatting corner left: the two implementations here are about a hundred lines each. The existing `pyjson` module in the core was checked and not reused: it reproduces Python's `json.dumps` (ASCII escapes, float forms, Python separators), which is a different byte string. A binary encoding (CBOR, as COSE uses) was the alternative; JSON was kept because the person and a third party can read the file they are asked to trust.
2. **The device id is the public key.** A record then needs no directory to be verified, and an export is self-contained. Thirty-two bytes per record is the cost.
3. **The signature covers `summary_hash`, not `summary`.** So a summary can be removed after signing and the record stays verifiable, chained and provable. The alternative, signing the text, would force a choice between sharing every summary and sharing nothing checkable. The commitment is salted (section 9); an unsalted hash of a short sentence would be guessable and redaction would be for show.
4. **The salt is derived, not random.** `append` stays a pure function (no random source in the core, reproducible vectors, deterministic signatures end to end). The salt is secret to anyone without the reference key, which is the property redaction needs.
5. **The record hash covers the signature; the signature covers a domain-separated hash.** `prev` and the Merkle leaves then name one signed record, and a signature made for a record cannot be replayed as a checkpoint signature or in any other Muse protocol that signs with the device key.
6. **The genesis value depends on the device.** A first record cannot be moved to another device's chain even before signatures are looked at.
7. **Hex, lower-case only, for every hash, key and signature.** One spelling per value: a bit flip that changes `a` to `A` is detected as malformed instead of being silently equivalent.
8. **Time tolerance 120 s, neighbours only.** Large enough for clock corrections and races between processes, small enough that an out-of-order time is visibly an anomaly. The home's store additionally never writes a time below the head's, so a clock set back does not break its chain.
9. **Signature before sequence and linkage in `verify_chain`.** Everything reported after a signature check concerns records the device signed, which is what makes `fork` and `wrong_prev` meaningful as evidence.
10. **Strict reading.** Repeated keys, `-0`, exponent forms and lone surrogates are where JSON parsers differ; refusing them removes the chance that two verifiers see two different records in one file.
11. **Key-shaped text is refused at writing time only.** The list of prefixes will change; if it were part of verification, old valid records would turn invalid.
12. **`egress` is an optional signed member, and version 1 was amended rather than replaced.** The Records screen needs to answer "what left my devices" from the record itself, and a claim like that is only worth showing if it cannot be edited afterwards, so it is signed. It is optional and left out when absent (like `links`), so every record made before the amendment keeps its bytes, signature and hash, and no second format version has to be carried for records that never left a development machine. The cost is that "absent" has to be read as "not stated" for ever, which is also the honest reading. It is one value, not a list: the filter and an export work per record, and one destination per record keeps both simple.

---

## 12. Limits, stated plainly

- **Tail truncation.** Removing the newest records leaves a valid chain. It is detected only against a checkpoint taken earlier, and by an attacker-proof margin only if that checkpoint is kept off the device (anchoring: not built).
- **A stolen device key** can sign anything, including a second history. Forks then become visible wherever the two histories meet, but nothing in the format prevents them. Key custody is the vault's job; the home currently keeps its key in a 0600 file.
- **Time is the device's own claim.** Nothing here proves when a record was made; that also needs an anchor.
- **The reference key is per vault.** Until the vault exists the home has its own key, so references made on the home cannot be matched with another device's.
- **Summaries are visible** wherever the records go unredacted, and the key-shape guard is best effort.
- **`egress` is what the writer says.** The signature proves the device wrote it and that nobody changed it since; it does not prove the content went only there. The home fills it in for what it does itself. A host bot's own reports (`POST /v1/receipts`) carry whatever the host said, or nothing, and Muse cannot see what a host does with its own tools. Records without `egress` are "not stated", not "stayed".
- **Unicode versions.** NFC is stable for all assigned characters, so signers on different Unicode versions agree except for text containing code points one of them does not know yet; verification never normalises, so this cannot make a valid record fail, only make two signers write different bytes for such text.
- **Not covered by the conformance vectors:** Unicode normalisation beyond the cases listed there; Ed25519 edge cases beyond the 64 listed; JSON nesting deeper than 128 (both refuse, by different mechanisms); chains longer than about a thousand records.

---

## 13. Mapping to FHIR AuditEvent and ISO/IEC TS 27560 (informative)

For the health add-on, which emits the same entry as a FHIR AuditEvent and treats permission records as consent receipts. This table is a starting point written without the standards' text at hand; element names are FHIR R4/R5 and the 27560 record structure as commonly described, and must be checked against the editions adopted when the add-on is built.

| Record | FHIR AuditEvent (R4; R5 name where it differs) | ISO/IEC TS 27560 consent record / receipt |
|---|---|---|
| record hash | `AuditEvent.id` (or an `identifier` extension) | `receipt_id` / `record_id` |
| `v` | `meta.profile` | `schema_version` |
| `device` | `source.observer`; `agent.who` for the acting device | `entity_id` of the recording party (party identification) |
| `seq`, `prev` | extension (chain position, previous hash) | none (integrity is left to the implementation) |
| `time` | `recorded` | `event_time` |
| `mandate` | `agent.policy` (R5: `authorization` holds the purpose) | the consent record the event belongs to; its purposes, controller and lawful basis live in the mandate, not here |
| `actor` | `agent.type`, `agent.requestor` (`person` = requestor true) | `event` originator (PII principal for `person`) |
| `action` | `type` / `subtype` (R5: `category` / `code`); `action` C/R/U/D/E derived from the name | `event_type` for `mandate.issue` / `mandate.revoke`; other actions are not consent events |
| `resource` | `entity.what` as an `Identifier` whose value is the keyed reference; never a literal reference | none (the receipt names categories of data, held in the mandate) |
| `summary` | `outcomeDesc` (R5: `outcome.detail`) or `entity.description` | none |
| `outcome` | `outcome`: `ok` → 0, `declined` → 4, `failed` → 8; `undone` → 0 on the undo event | `event_state` (given, withdrawn) for consent events |
| `cost` | extension | none |
| `egress` | `entity` with a role of "destination" (R5: an `agent` of type "destination" with `agent.networkUri` or `who`) | the recipient / third-party field of a data-sharing event, when the action is one |
| `links.job`, `links.card` | `entity` with role "job" / `agent.policy` | none |
| `links.undo_of` | `entity.what` pointing at the earlier AuditEvent | the earlier record's `record_id` on a withdrawal |
| `sig`, checkpoint | `Provenance.signature` over the AuditEvent, or carried as an extension | the receipt's signature; the anchored checkpoint is the "proof" row of architecture 2.8 |
