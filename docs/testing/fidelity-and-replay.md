# Fidelity and replay

Record a provider's answer once, then replay it. Router tests and the chores-check gate can then run with the network closed, at no cost and with no use of a free-tier quota.

The shape of this is the one AWS's emulator uses: a test is checked against the real system first, and after that it is replayed. The note on that method is in `docs/reviews/ecosystem-scan-2026-11-13.md`. The gate-1 run that spent the allowances (`docs/reviews/gate1-full-2026-11-13.md`) is why the replay exists: about 900 requests in 78 minutes, and the late research-report tasks failed once the Gemini embedding alias returned 403.

## Two ways to use one cassette

A cassette is a JSON file under a directory you choose. `tests/cassettes/` holds the examples that are committed. Each file has:

- a **fingerprint** of the request: method, path, a sha256 of the raw body, and a few fields (model, whether it streamed, `max_tokens`, how many messages). The prompt text is not stored.
- the **response**: status, headers, body, and the SSE chunks when the body is a stream. `elapsed_ms` is recorded and is not slept on replay.
- no API key, and no `Authorization`, `Cookie`, `x-api-key`, or `x-goog-api-key` header. Key-shaped text in a response is replaced with `[redacted]` before the file is written.

`tests/test_cassette.py` fails if any file under `tests/cassettes/` contains a key-shaped string or one of those headers.

**At the provider boundary.** `MUSE_PROVIDER_MODE` is `live` (the default), `record`, or `replay`. `live` leaves every base URL as the catalogue set it. `record` and `replay` need `MUSE_CASSETTE_DIR`. The hook (`muse/hook.py`, on the deployment call, after the output floor) points that one call at a local proxy (`muse/cassette.py`). In `record` the proxy forwards to the original base URL, which it receives in `x-muse-cassette-upstream` and then strips, and writes the cassette. In `replay` it answers from the directory and opens no outbound connection. A request with no cassette is HTTP 404. If replay was asked for and the proxy cannot start, the call is refused rather than sent to a real provider.

**At the gate.** `scripts/chores_check.py --record DIR` still talks to whatever router `MUSE_PROXY_URL` names, and writes one cassette per exchange. `--replay DIR` points the skill runner at the local proxy, so the router process is not involved and no quota is touched. Pass one of the two, then the other. The same files work for both.

## Refreshing the cassettes

On a machine that has the provider keys:

```bash
# the router, once, against the real providers
MUSE_PROVIDER_MODE=record MUSE_CASSETTE_DIR=tests/cassettes/router muse up

# or the gate, once, against a running router
python3 scripts/chores_check.py --yes --sample 5 --seed 0 --record tests/cassettes/gate
```

Then:

1. Read the diff. A cassette holds response text. It must not hold a prompt, a key, or an auth header.
2. `python3 -c "from pathlib import Path; from muse.cassette import secret_problems; print(secret_problems(Path('tests/cassettes')))"` prints nothing when the directory is clean. `tests/test_cassette.py` runs the same check.
3. Commit the directory. CI stays on `replay` and on `muse/fakeprov`. It does not call Groq, Gemini, or OpenRouter.

Replay matches on the hash of the raw request body. A change to a prompt, a skill, or `max_tokens` is a new request and a miss (HTTP 404) until that call is recorded again. Record the new shape, review it, and replace the old file.

## The fake provider

`muse/fakeprov` is an OpenAI-compatible server on 127.0.0.1 with a script per model name. It is what the router tests call, so those tests never need a cassette from a real provider and never spend a quota.

```bash
python3 -m muse.fakeprov
```

`MUSE_FAKE_SCENARIOS` is `model=scenario,model=scenario`. The scenarios are: `ok`, `slow`, `empty_reasoning`, `reasoning_below:<N>`, `429_retry_after:<seconds>`, `429_groq_rpm`, `429_groq_tpd`, `429_gemini_day`, `429_gemini_minute`, `429_unclassified`, `429_bare`, `403_quota`, `sse_error`.

`tests/test_router_fidelity.py` plays those responses through `muse.quota.classify_failure` and the ledger, which is the circuit breaker the hook writes. `tests/integration/test_fakeprov_router.py` points a real LiteLLM proxy at the same server and checks fallbacks, the breaker, the output floor, and the two gaps below. The integration file is skipped unless `litellm` is installed, which is the proxy job in CI, not the unit job.

## What the router does with those shapes

| Shape | What happens |
|---|---|
| 429 with `Retry-After`, or "try again in …" | That upstream is cooled for the stated time. The alias falls through to the next deployment. The next request skips the cooled one. |
| 429 whose text says "quota" or "limit" and names no clock | That upstream is exhausted until the estimated daily reset: midnight Pacific for Gemini, UTC midnight otherwise. A bare 429, with neither word, stays a 60 second cooldown, and so does the wording of a per-minute limit ("rate limit", "too many requests", "per minute", RPM, TPM) unless it also says "quota": several services report a per-minute limit with no clock, and one such reply must not take a service out until midnight. This is the fix for the first gap in the PR #8 review. |
| Gemini PerDay (`quotaId` … `PerDay`) | The one model is exhausted until midnight Pacific. A second Gemini model in the same alias still answers. The catalogue says the limit is per project and per model, so the block stays on that upstream until a captured response shows the cap is project-wide. The test that wants provider scope is `xfail(strict=True)`. |
| Error object inside an HTTP 200 SSE stream | The stream is a success as far as the hook is concerned, so `classify_failure` never sees it, the upstream stays eligible, and the fallback is not taken. The test that wants a block is `xfail(strict=True)`. Confirm the frame with a captured stream before teaching the hook to read it. |
| 403 whose message says the quota is used up | The bad-key rule: the whole provider, for 15 minutes. The next request skips every model of that provider. |
| Empty content, reasoning tokens spent, small `max_tokens` | The output floor in `muse/hook.py` raises the cap to the deployment's `min_output_tokens` (1,024 on the gpt-oss deployments) before the call is sent. The fake returns text once the cap is at least that floor. |

`strict=True` means a test that starts passing fails CI until the mark is removed. That is the point: a fix should be a deliberate change, with the mark deleted in the same commit.

## A no-regression gate

The chores report already scores each run twice. G1 counts the skill's own check. `rate_independent` counts answers that contain the task's expected facts. The gate-1 write-up is to judge on the facts.

A replay makes a fixed sample free to repeat:

1. Record the sample once (`--record`, same `--sample` and `--seed`).
2. On each change that can affect an answer, run `--replay` on that directory.
3. Compare `rate_independent` (and the task ids that were wrong) with the last accepted report for the same `set` hash. A drop fails the check.

Replay checks that the skills still do the same thing with the same model answers. It does not check that a new model is as good as the old one. That comparison stays a real run, on a small capped sample, on a machine that has keys. The nightly real run stays capped. The merge rule to add, once a baseline report is committed: the replayed sample's fact accuracy must not fall, and a real run's fact accuracy on the agreed sample must not fall either.
