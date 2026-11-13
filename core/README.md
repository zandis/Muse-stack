# muse-core

The portable core of Muse: the pure rules from the Python package (`muse/`),
in Rust, for the desktop (Tauri) and phone apps. It does no I/O: no network,
no files, no clock (`now` is always an argument), no async, no `unsafe`.

Each function returns what the Python function of the same name returns for
the same input. That is checked, not promised: `core/conformance/*.json` holds
8,198 inputs with the answers Python gave (5,148 for the ported rules, 3,050 for records), and `cargo test` replays all of them
(floats must match bit for bit).
5,148 inputs with the answers Python gave, and `cargo test` replays all of them
(floats must match bit for bit). Permissions add 584 more of a different kind
(see "Permissions" below); signing a permission is also the one place the crate
uses randomness, for the fresh key of each signed block.

## Run it

```sh
cd core
cargo test                       # every vector, plus the tz database cross-check
cargo clippy --all-targets -- -D warnings
```

Regenerate the vectors after changing `muse/quota.py`, `muse/ration.py` or
`muse/cache.py` (from the repository root, with the repo's Python environment):

```sh
python scripts/export_conformance.py           # rewrites core/conformance/
python scripts/export_conformance.py --check   # exit 1 if a file is out of date
```

`tests/test_conformance_export.py` (part of the normal pytest run) fails when
the committed files are not what Python returns today, so a Python change
cannot drift away from the core unnoticed: regenerate, then `cargo test` shows
what the core has to learn. The export is deterministic; Python 3.11, 3.12 and
3.13 produce the same bytes.

Vector format, one per line: `{"name", "fn", "input", "expected"}`.
`expected` is `{"ok": value}` or `{"err": "<Python exception>"}`. A float JSON
cannot hold is `{"$float": "nan" | "inf" | "-inf"}`.

## What is ported

| Python | Rust | Vectors |
| --- | --- | --- |
| `quota.parse_retry_after`, `parse_duration`, `parse_reset` | `quota::` same names | 139, 86, 61 |
| `quota.next_midnight_pacific`, `next_midnight_utc`, `next_month_utc`, `month_start`, `day_start`, `iso` | `quota::` same names | 546, 40, 64, 64, 173, 40 |
| `quota.classify_failure` (and `Block`, `normalise_headers`) | `quota::classify_failure` | 201 |
| `ration.previous_midnight_pacific`, `window_bounds`, `window_reset` | `ration::` same names | 546, 747, 453 |
| `ration.burn_rate`, `pace_position`, `pace_release_at`, `pace_holds` (and `Limit`, `Pace`, `Settings`) | `ration::` same names | 30, 494, 684, 348 |
| `ration.options_trailer`, `parse_options` | `ration::` same names | 33, 59 |
| `cache.cacheable`, `cache_key` | `cache::` same names | 236, 104 |
| `muse.records` (see "Records" below) | `records::` same names | 3,050 in `records.json` |
| `muse.permissions` (`propose_from_fields`, `issue`, `narrow`, `check`, `describe`, `RevocationList`, `SpentSet`) | `permissions::` same names | 584 (see below) |

Supporting modules: `pyjson` (JSON read and written by Python's rules, needed
for the cache key and for finding JSON inside error messages) and `pytext`
(`str.strip`, `int()`, `float()`, `str.isdigit`, `repr(float)`).

Dependencies: `serde`, `serde_json` (feature `float_roundtrip`), `sha2`; for records also `ed25519-dalek`, `hmac`, `unicode-normalization`.
### Permissions

`permissions` is the one module that is not a port of plain arithmetic: a permission is a
signed [Biscuit](https://www.biscuitsec.org/) token (`docs/spec/interfaces/permission-token.md`).
Rust is the reference here and Python (`muse/permissions`, over `biscuit-python`) is the twin.
Every block of a token is signed with a fresh random key, so the two sides can never produce
the same bytes. `conformance/permissions.json` therefore carries tokens made once with
test-only keys (`conformance/fixtures/`), and both sides must read them the same way:
the same decision and reason for each request, the same sentence in English and Traditional
Chinese, the same revocation identifiers. `tests/permissions_vectors.rs` replays the file;
`tests/permissions.rs` holds the property tests (a narrowed token never allows what its
parent refuses; a block that tries to widen changes nothing).

```sh
cargo test --test permissions --test permissions_vectors
# only when the schema changes (new random tokens, then re-export from Python):
cargo test --test permissions_vectors -- --ignored generate_fixture_tokens
python scripts/export_conformance.py --make-python-fixtures && python scripts/export_conformance.py
```

Recomputing `permissions.json` needs `pip install "biscuit-python==0.4.0"`; without it the
exporter leaves that file as it is and says so.

Dependencies: `serde`, `serde_json` (feature `float_roundtrip`), `sha2`, and for permissions
`biscuit-auth` pinned to `=6.0.0` (default features off, `datalog-macro` on), which brings
the Ed25519 and P-256 crates it signs with. This crate adds no cryptography of its own.
`chrono` and `chrono-tz` are dev-dependencies only, used by
`tests/pacific_tzdata.rs` to check the Pacific midnight rule against the tz
database; the library itself uses the same arithmetic rule as the Python (see
Finding 1 for why it is not tzdata).

## Records

`records` is the signed, per-device record chain (format:
`docs/spec/interfaces/record-format.md`). Unlike the modules above it was
written in both languages at once, so its vectors come from the Python twin
(`muse/records/vectors.py`, with fixed, public, test-only keys) and are
replayed by `tests/records_conformance.rs`; `tests/records_props.rs` holds the
properties (any single-bit change is detected, a proof verifies only for its
own record). Signatures must match byte for byte: Ed25519 is deterministic.

| Function | Vectors |
| --- | --- |
| `jcs::canonicalize`, `jcs::loads_strict` (RFC 8785, integers only; strict reading) | 59, 39 |
| `check_summary`, `key_shaped`, `resource_ref`, `summary_salt`, `summary_hash`, `genesis` | 59, 30, 19, 11, 9, 5 |
| `append`, `canonical_bytes`, `hash`, `redact` | 143, 27, 22, 15 |
| `verify_record`, `verify_signature`, `verify_chain` | 117, 64, 108 |
| `merkle_root`, `inclusion_proof`, `verify_inclusion`, `consistency_proof`, `verify_consistency` | 77, 614, 638, 682, 166 |
| `checkpoint`, `verify_checkpoint`, `export`, `export_text`, `verify_export`, `verify_export_text` | 15, 17, 21, 3, 69, 21 |

89 of these (the last ones in the file) are for `egress`, the optional signed
member added to format v1 (record-format.md section 1.2): its allowed values,
its bytes, and that changing, adding or removing it breaks the signature.
`records::left_devices(&record)` is the reader's rule for the "Left my devices"
filter. Every vector that existed before is byte for byte what it was.

Storage is not here: `records::RecordStore` is the trait the vault will
implement, and `MemoryStore` is for tests. Keys come in through the `Signer`
and `RefKey` traits. Extra dependencies: `ed25519-dalek` 2, `hmac`,
`unicode-normalization` (and `sha2`, already present). `Cargo.toml` raises the
optimisation level of `curve25519-dalek` and `sha2` in dev and test builds,
because the tests verify tens of thousands of signatures.

Where the two sides could still differ, outside what the vectors pin: NFC for
code points that one side's Unicode tables do not know yet (Python 3.13 has
Unicode 15.1; the `unicode-normalization` crate follows its own release);
Ed25519 edge cases beyond the 64 listed (OpenSSL behind Python, ed25519-dalek
`verify` here); JSON nested between 128 and Python's recursion limit (both
refuse, at different steps). The records code uses `Option::is_none_or` and
`is_multiple_of`, so it needs Rust 1.87 or newer.

## Next (not in this slice)

- `quota.parse_limit_headers` / `ObservedLimit`.
- `ration`: `Settings.from_env`, `parse_paid_cap`, `format_usd` (Decimal),
  `resolve_tz` / `local_day_bounds` (local time zone), `declared_quota`,
  `known_limits`, `limit_rate`, `pace_view`, `rank_upstream`, `rank_values`,
  `prefer`, `expiring`, `reserve_fraction`, `breaches_reserve`, `app_cap`,
  `paid_gate`, `ration()`, `decide()`, `available_options`, `with_options`,
  `fail_closed`.
- `cache`: `CacheSettings.from_env`, the store (`ExactCache`), `on_disk_allowed`,
  `answer_text`, `mock_response`.
- The rules engine, the ledger, privacy policy, and everything with I/O.

## Where Rust differs on purpose

1. **Argument types.** Python accepts any object and calls `str()` on it; Rust
   takes text. `parse_retry_after`, `parse_duration`, `parse_reset` take
   `Option<&str>`; headers are an ordered list of `(name, Option<value>)` pairs
   (a later duplicate wins, as in a dict); `classify_failure` takes
   `status: Option<i64>` (pass `None` for anything that is not an integer; Python
   ignores bools, floats and strings the same way). The conformance test does
   the `str()` step for vectors whose value is a number.
2. **Exceptions are `Err(PyError)`.** Functions that can raise in Python return
   `Result`. Every "timestamp does not fit a datetime" failure is one variant,
   `TimeOutOfRange`; Python raises `ValueError`, `OverflowError` or `OSError`
   for these depending on the value and platform (the exporter writes
   `"TimeOutOfRange"` for all three).
3. **JSON nesting depth.** Python raises `RecursionError` from
   `classify_failure` when a body holds JSON nested about 10,000 levels deep
   (CPython 3.13; the depth depends on the interpreter). Rust returns
   `Err(PyError::Recursion)` beyond 1,000 levels (`pyjson::MAX_DEPTH`). Between
   the two depths Python answers and Rust errors. Tested in `tests/units.rs`,
   not in the vectors.
4. **Lone surrogates.** A Python string can hold `"\ud83d"` alone; a Rust string
   cannot. `PyJson::parse` turns a lone surrogate escape into U+FFFD. Nothing
   in `classify_failure` can tell (covered by a vector). For `cache_key` Python
   raises `UnicodeEncodeError` on such a message (Finding 12) where Rust
   returns a key.
5. **Integers are i64 in `Limit` and `burn_rate`.** Python's are unbounded.
   Results are identical while `limit` and `remaining` are below 2^53.
6. **`Settings` is a subset.** It has the fields the pacing functions read and
   the other float/bool/string fields; `paid_cap_usd` (a Decimal), `tz` and
   `warnings` come with the rules. The conformance test fails if Python's
   `Settings` gains a field the Rust struct lacks.
7. **Request bodies are `PyJson`.** `cacheable` and `cache_key` take the
   crate's own JSON value because serde_json has already lost what Python's
   key depends on: it reads `-0` as a float (Python: the integer 0) and
   integers above u64 as floats (Python: exact). Use `PyJson::parse(text)` on
   the request text for an exact key; `PyJson::from(&serde_json::Value)` is
   exact for everything else. A body that is not an object is treated as an
   empty one (Python would raise `AttributeError`).

## Findings

Python behaviour that was surprising, ambiguous, or a likely bug. Nothing was
changed in `muse/`; Rust matches each of these unless "Where Rust differs"
says otherwise. Line numbers are in the files as of this slice.

1. **Pacific time uses the 2007 rule for every year** (`muse/quota.py:172-180`).
   `_pacific_offset_hours` computes DST from "2nd Sunday of March, 1st Sunday
   of November" without tzdata. For 2007 onward this equals the tz database
   (checked every 37 minutes from 2007 to 2050, and each second around every
   switch, in `tests/pacific_tzdata.rs`). Before 2007 it does not (e.g. 20 March
   2006). Rust uses the same rule, so `chrono-tz` is not a runtime dependency.
2. **`day_start(now, "gemini")` is an hour off on the two switch days**
   (`muse/quota.py:208-212`): it is "next Pacific midnight minus 24 h", and
   those days are 23 and 25 hours long. The comment says "close enough".
   `ration.previous_midnight_pacific` is exact. `window_reset("day", "gemini")`
   adds the 24 h back, so it is exact too.
3. **`classify_failure` can raise.** (a) `RecursionError` on deeply nested JSON
   in the body: `_json_candidates` catches only `ValueError`
   (`muse/quota.py:262-274`), and `_signals` runs before the status is looked at
   (`muse/quota.py:367`), so even a 401 or a 500 with such a body raises.
   (b) A `now` outside the years 1..9999 raises from the calendar helpers when
   an estimated reset is needed (`muse/quota.py:183-205`).
4. **A NaN clock is not rejected.** `classify_failure(..., now=nan)` returns a
   `Block` whose `until` is NaN for 401/402/403 and plain 429
   (`muse/quota.py:332-333`); `parse_retry_after("30", nan)` returns 30.0.
5. **`iso()` does not pad years below 1000** on glibc (`muse/quota.py:224-228`):
   `strftime("%Y")` gives `385-07-25T07:06:40Z`. Other C libraries pad. Rust
   follows glibc; this is not in the vectors (it is in `tests/units.rs`).
6. **Retry-After dates are read far more leniently than HTTP allows**
   (`muse/quota.py:125-135`). `email.utils.parsedate_to_datetime` accepts
   `Nov 12 2026 07:01:00`, two-digit years, `07.01.30`, named US zones (`PST`),
   underscores (`1_2 Nov`) and non-ASCII digits (`Thu, ١٢ Nov 2026 ...` gives
   60.0), although the docstring says non-ASCII digits give None; that holds
   only for delta-seconds and for a value that *starts* with a digit. Rust
   ports the standard-library reader step for step, with digit tables from
   Unicode 15.1 (CPython 3.13); a Python with a newer Unicode database could
   accept digits this table does not have.
7. **OpenRouter per-minute: zero is treated as missing** (`muse/quota.py:412-413`).
   `wait if wait else DEFAULT` turns `Retry-After: 0` into a 60 s cooldown, and
   `(reset - now) if reset else None` does the same for a reset of 0.0.
8. **`quotaId` / `quotaMetric` that are not strings are searched as their
   Python repr** (`muse/quota.py:303-306`), so `{"quotaId": {"PerDay": 1}}` or a
   list holding `"...PerDay"` counts as a daily quota at any depth, although
   `_walk` stops at depth 8 for objects. Rust searches the nested keys and
   strings, which is equivalent.
9. **A later `retryDelay` replaces an earlier one, even when it is unusable**
   (`muse/quota.py:308-309`): an object with `"retryDelay": "5s"` followed by
   another with `"retryDelay": null` or `"soon"` ends with no delay (then the
   text pattern is tried). A numeric `retryDelay`
   is accepted through `str()`.
10. **`window_reset` treats every unknown window name as a month**
    (`muse/ration.py:352-358`): `window_reset("week", ...)` is the next UTC
    month; `window_bounds("week", ...)` is None.
11. **`pace_release_at` and `pace_position` can raise** (`muse/ration.py:519`,
    `527-529`). `pace_release_at` asserts a window start and reset
    (`AssertionError`, or `TypeError` under `python -O`) and divides by the
    limit (`ZeroDivisionError` for 0). `pace_position` guards the limit for the
    remaining fraction (line 509) but not for the projected leftover (line 519),
    so a paced limit of 0 with a rate raises `ZeroDivisionError`.
12. **`cache_key` raises `UnicodeEncodeError` for a lone surrogate**
    (`muse/cache.py:110-111`), which `json.loads` produces from `"\ud83d"` in a
    request. Also: `temperature: 0` and `temperature: 0.0` give different keys
    (`0` vs `0.0` in the canonical JSON) although `cacheable` treats them alike,
    so two clients that differ only in that never share an entry; NaN and
    Infinity are written as bare tokens.
13. **`cacheable` compares with `==`** (`muse/cache.py:84-97`). `value not in
    (None, False, [], {})` counts `0` and `0.0` as unset but `""` as set;
    `n not in (None, 1)` accepts `True` and `1.0`; `float(temperature)` accepts
    text (`"0"` is cacheable, also `"٠"` and `" 0_0 "`) and bools (`False` is
    temperature 0), and raises an uncaught `OverflowError` for an integer too
    large for a float.
14. **`parse_options` raises `ValueError` on a malformed `pay:`**
    (`muse/ration.py:747-748`) and accepts `pay:nan`, `pay:inf`, underscores and
    non-ASCII digits. `handoff:anything` counts as `handoff`.
15. **Rule numbers disagree inside `muse/ration.py`.** The module docstring
    numbers pacing 7, hand-off 8, one-off 9 (lines 36-44); the section comments
    and docstrings say "pacing (rule 4)" (417), "Rule 4's ordering hint" (667),
    "rule 6" (716) and "Rule 7" for one-off (786). Documentation only.

## Not verified

- CI itself (`.github/workflows/ci.yml`, job `core`) has not run; it assumes
  `cargo` is on `ubuntu-latest`. Locally: cargo 1.97.0.
- No minimum Rust version is declared. The code needs 1.77 (`f64::round_ties_even`);
  only 1.97.0 was tried.
- Only Linux/glibc CPython was used to export. Findings 5 and 6 are where
  another platform or Python version could answer differently.
