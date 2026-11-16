# dsh-our-free-model: what to borrow

2026-11-13. Read: [Ebony-Vinyl/dsh-our-free-model](https://github.com/Ebony-Vinyl/dsh-our-free-model) v2.8.3 (MIT, copyright 2026 Ebony-Vinyl), cloned read-only, and this tree (LiteLLM 1.108.5, `muse/hook.py`, `muse/quota.py`, `config/providers.yaml`, `scripts/render_config.py`).

Do not install the plugin. Its `package.json` describes account channels, login, an account pool, and credit claiming. The free lane is keyless and mints session ids; Kilo is a keyless pool; EAC is a sealed co-paid credential. Below is only whether four mechanisms are worth rebuilding on our own keys.

## Verdicts

| Idea | Verdict | Size |
|---|---|---|
| Named chains (`combo:<name>`), fallback / round-robin, switch only before a content token | later for the commit rule; skip the rest | L |
| 429 breaker (6h default, per lane, passive, no probing) | skip | — |
| Tool-output compression (RTK) in a pre-call hook | later, opt-in, dev-tool output only | M |
| `/v1/messages`, dashboard, reasoning-effort map | skip; charts later only if the page is hard to read | S |

## 1. Named chains

`src/combos.js` advertises `combo:<slug>`. `adapter.runCombo` walks it: `fallback` starts at the head; `round-robin` rotates an in-memory cursor for `stickyLimit` turns; `smart` / `usage` / `pace` reorder from quota readings. Vision is the intersection; context and max output are the minimum. `commitsAnswer` (`src/adapter.js`) holds chunks until a non-empty text delta or a tool-call delta. Reasoning deltas do not commit, so "thought, then 429" can still switch. Silence for 25s (`CHAIN_SILENCE_MS`) abandons the candidate. After commit, the failure is the caller's.

Our aliases are already those virtual models. `fast`, `cheap`, `smart`, and `long-context` get LiteLLM `fallbacks` and `context_window_fallbacks` from `scripts/render_config.py`. Inside a group: `order`, then `simple-shuffle` by `rpm`. `muse/ration.py` `prefer` is their pace/usage sort. `RateLimitErrorRetries` is 0, so a 429 leaves the group. Alias membership is hand-curated (our stand-in for the intersection rule); overflow already goes to `long-context`.

LiteLLM fallbacks run when the attempt raises, before a response is returned. Flushed SSE cannot be unsent. Nothing here tests "reasoning frames, then 429, client sees only the next model." Their cursor is process-local and would split across our workers.

Skip `combo:` names, round-robin, and smart/usage/pace. The commit rule is the gap, and it is large: `muse/hook.py` is too late to unsend bytes, so the buffer has to sit on the SSE flush. Build it only after a captured Groq or Gemini trace shows reasoning followed by 429.

Test plan: fake upstream streams reasoning then 429 and the client sees only the next deployment; a content token then 429 is returned and not replayed; privacy still cannot fall through; 25s of silence does not sit out the 120s router timeout.

License: reimplement. Copied lines need the MIT notice.

## 2. 429 breaker

`src/core/runtime.js` keeps one bit for the whole keyless lane. `LANE_QUOTA_DEFAULT_MS` is 6h. One real 429 sets `retryAt` from `Retry-After` or that default; later turns are refused locally; a success or an egress change clears it; probes stay off. `FreeTierError` is a `locked` state waiting does not clear. Separately, `src/route/classify.js` caps a provider reset at 30 minutes (`MAX_PROVIDER_COOLDOWN_MS`). That cap would cut off our daily blocks.

We already do the keyed-provider version. `muse/quota.py` writes a ledger block; `async_filter_deployments` drops it before the call, across aliases, workers, and restarts. Gemini PerDay waits until Pacific midnight, OpenRouter `free-models-per-day` is provider-scoped, and a `Retry-After` over an hour (`LONG_RETRY_AFTER`) is exhaustion. No header and no matching text: 60s (`DEFAULT_COOLDOWN_SECONDS`), and LiteLLM's own no-header cooldown is `MUSE_COOLDOWN_FALLBACK_S` (default 30). `background_health_checks` is false. `muse status --probe` is manual and only hits OpenRouter's quota endpoint.

A 6h default fits an IP bucket that omits `Retry-After`. On Groq it would freeze an RPM blip. Leave it out.

Gaps, all small, all in the classifier or the failure hook:

1. An unclassified 429 is retried in about a minute, so a daily cap whose body matches no parser gets hammered. Widen the wait only when the text says quota or limit and names no clock. A bare 429 stays at 60s.
2. Gemini PerDay is `scope: upstream` (one model). The catalogue says limits are per project and per model, so one Gemini 429 leaves the other Gemini deployments in `fast` / `cheap` / `smart` eligible. OpenRouter's account-wide strings already use provider scope. Match that once a capture shows the cap is project-wide.
3. Blocks come from `async_post_call_failure_deployment_hook`. An error object inside an HTTP 200 stream never reaches `classify_failure`. Their `readStream` throws on `payload.error`. Confirm with one captured stream before teaching the hook.

Test plan: extend `tests/test_quota.py` — quota wording and no clock gets a long block; a bare 429 stays at 60s; a project-scoped Gemini PerDay body blocks every Gemini upstream.

License: nothing to copy. Do not port the 30-minute cap.

## 3. Tool-output compression

`src/token-saver/rtk/` rewrites tool-result text only. Errors are kept. The replacement must be non-empty and smaller. Under 500 bytes or over 10 MiB passes through. A thrown filter sends the original. The diff filter keeps 100 lines per hunk, caps at 500 lines, and writes `... (N lines truncated)`. Their README's "~90% off a 40 KB git diff" was not remeasured; those caps make a cut that size ordinary, and the model answers from the truncated text.

`async_pre_call_hook` already holds the messages. A compactor would be `muse/compact.py`, called from that hook, default off, with a byte count on the call and a response header.

Worth it for `cline` / `continue` / `aider`, which paste git, grep, and build logs. A long user draft, or a file the files-answer skill read, must stay intact: a numbered clinical note matches their `read-numbered` and `smart-truncate` filters, and prose that looks like a diff would become a different question. Their `caveman` and `ponytail` families (`src/token-saver/prompts.js`) rewrite the system prompt. Leave those out.

Verdict: later. Opt-in per app, off for `private` and `local`, off for files-answer, truncation marker required, `x-muse-compact` on the response. Size M.

Test plan: fixture diff and grep with a known ratio and a stable marker; a tool error unchanged; a user message that looks like a diff unchanged; a `private` request unchanged; a filter exception leaves the body byte-identical.

License: comments say the filters port a Rust RTK tool and that the markers are the contract. MIT covers their JS; the Rust license is not in the tree. Reimplement. Do not paste `src/token-saver/rtk/`.

## 4. Other ideas, and what to refuse

Skip a hand-rolled `POST /v1/messages`. `src/anthropic.js` translates the shape, and `count_tokens` is a local estimate with no tokenizer. The hook already handles LiteLLM's `anthropic_messages` call type for the output floor. Use the proxy's own route if a client needs it.

`muse dash` already shows the ledger (tokens, whole-call `latency_ms`, cost; no prompt text). Their heatmap and tokens/s are presentation. Time-to-first-token is not stored. Charts later, size S, only if the page is hard to read.

`src/effort.js` maps Light/Balanced/Deep to 2048/8192/full and doubles that when thinking cannot turn off, because their gateway ignores `reasoning_effort` and only enforces `max_tokens`. We already raise a too-small cap to `min_output_tokens` (256 on gpt-oss, measured here) and set `drop_params: true`. A second ladder would fight the floor. Skip until a keyed provider documents an effort enum we drop and a caller depends on it. Size S, beside the floor in `muse/hook.py`.

Refuse: remote announcements (`src/feed.js` polls GitHub raw and jsDelivr for owner-pushed HTML, including an urgent modal); self-update (`src/updater.js` replaces files and hot-reloads); bundled binaries (`src/engine-parts/win32-x64.*.part`, magpie); a center that installs remote extension code; client impersonation (`src/o2a/` injects an official prompt and tool list; `src/upstream.js` sends a vendor User-Agent); an egress proxy used to clear an IP quota; canned answers in `src/local-optimize.js`; the account-bench cooldowns in `src/route/classify.js`; either npm package (their README says unscoped `dsh-our-free-model` is a third-party repack).

No product code in this note. No lines from their tree are copied.
