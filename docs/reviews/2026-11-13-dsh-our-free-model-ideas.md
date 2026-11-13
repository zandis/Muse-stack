# dsh-our-free-model: what to borrow

2026-11-13. Source read: [Ebony-Vinyl/dsh-our-free-model](https://github.com/Ebony-Vinyl/dsh-our-free-model) v2.8.3 (MIT, copyright 2026 Ebony-Vinyl), cloned read-only. Ours: this tree (LiteLLM 1.108.5, `muse/hook.py`, `muse/quota.py`, `config/providers.yaml`, `scripts/render_config.py`).

Do not install or vendor the plugin. Its `package.json` describes account channels, login, an account pool, and credit claiming. The free lane dials a keyless gateway and mints session ids; the Kilo lane is a keyless pool; EAC is a sealed co-paid credential. This note only asks whether four mechanisms are worth rebuilding on our own keys.

## Verdicts

| Idea | Verdict | Size |
|---|---|---|
| Named chains (`combo:<name>`), fallback and round-robin, switch only before a content token | later for the commit rule; skip the rest | L |
| 429 circuit breaker (6h default, per lane, passive) | skip | — |
| Tool-output compression (RTK) in a pre-call hook | later, opt-in, dev-tool output only | M |
| `/v1/messages`, usage dashboard, reasoning-effort map | skip; dashboard charts later if wanted | S |

## 1. Named chains

`src/combos.js` advertises a virtual model `combo:<slug>`. `adapter.runCombo` walks the list. `fallback` starts at the head. `round-robin` rotates an in-memory cursor and keeps it for `stickyLimit` turns. `smart`, `usage`, and `pace` reorder from quota readings. Advertised vision is the intersection; context and max output are the minimum. `commitsAnswer` in `src/adapter.js` holds chunks until a non-empty text delta or a tool-call delta. Reasoning deltas and a reasoning `block-end` do not commit, so a model that thinks and then 429s can still be abandoned. A candidate silent for 25s (`CHAIN_SILENCE_MS`) is abandoned. A failure after commit belongs to the caller.

Our aliases are already virtual models. `config/providers.yaml` defines `fast`, `cheap`, `smart`, and `long-context`. `scripts/render_config.py` writes LiteLLM `fallbacks` and `context_window_fallbacks`. Inside a group, `order` then `simple-shuffle` weighted by `rpm`. `muse/ration.py` `prefer` already reorders one order level by burn and surplus, which is their `pace` / `usage` sort. `RateLimitErrorRetries` is 0, so a 429 leaves the group.

LiteLLM fallbacks run when the attempt raises, before a response is returned. Once SSE bytes are flushed, the client has seen them. Nothing in this repo tests "reasoning frames, then 429, client sees only the next model." Their rotation map is process-local and dies on restart, so it would split across our workers. Alias membership is hand-curated, which is our stand-in for their intersection rule, and overflow already targets `long-context`.

Skip `combo:` names, round-robin, and the smart/usage/pace strategies. The commit rule is the one missing piece, and it is large: `muse/hook.py` sees the attempt after bytes can already have been flushed, so the buffer has to sit on the SSE flush. Build it only after a captured Groq or Gemini trace shows reasoning followed by 429.

Test plan, if built: a fake upstream streams reasoning frames then 429, and the client body contains only the next deployment's text; a content token then 429 is returned as an error and is not replayed; the privacy class still cannot fall through to a weaker provider; 25s of silence does not hold the caller for the 120s router timeout.

License: reimplement. Copying `combos.js` or `runCombo` requires the MIT notice.

## 2. 429 breaker

`src/core/runtime.js` keeps one bit for the whole keyless lane. `LANE_QUOTA_DEFAULT_MS` is 6 hours. One real 429 sets `retryAt` from `Retry-After` or that default; `laneGate` then refuses turns locally; a success or an egress change clears it; automatic probes stay off. `FreeTierError` is a separate `locked` state that waiting does not clear. `src/route/classify.js` is a second mechanism and caps a provider reset at 30 minutes (`MAX_PROVIDER_COOLDOWN_MS`) for candidate rotation. That cap would cut off our daily blocks.

The keyed-provider version is already here. `muse/quota.py` classifies a 429 into a ledger block. `async_filter_deployments` drops blocked upstreams before the call, across aliases, workers, and restarts. Gemini PerDay waits until Pacific midnight, OpenRouter `free-models-per-day` is provider-scoped, and a `Retry-After` longer than an hour (`LONG_RETRY_AFTER`) is treated as exhaustion. A 429 with no header and no matching text waits 60s (`DEFAULT_COOLDOWN_SECONDS`). LiteLLM's own no-header cooldown is `MUSE_COOLDOWN_FALLBACK_S` (default 30). `background_health_checks` is false. `muse status --probe` is manual and only calls OpenRouter's quota endpoint.

A 6h default fits an IP-bucket lane that omits `Retry-After`. On Groq it would freeze an RPM blip that omitted the header. Leave it out.

Three gaps, all small, none of them a new component:

1. An unclassified 429 is retried in about a minute. A daily cap whose body matches none of the parsers gets hammered. Widen the wait only when the text says quota or limit and names no clock. A bare 429 stays at 60s. Change lives in `classify_failure`.
2. Gemini PerDay is `scope: upstream` (one model). The catalogue says limits are per project and per model, so a project cap recorded on one Gemini deployment leaves the other Gemini deployments in `fast`, `cheap`, and `smart` eligible until each of them 429s. OpenRouter's account-wide strings already use provider scope. Use that scope for a Gemini body once a capture shows the cap is project-wide.
3. Blocks are written from `async_post_call_failure_deployment_hook`. An error object inside an HTTP 200 stream never reaches `classify_failure`. Their `readStream` throws on `payload.error`, so their breaker opens. Confirm with one captured stream before teaching the hook about it.

Test plan for (1) and (2): extend `tests/test_quota.py`. A quota-worded 429 with no clock gets a long block; a bare 429 stays at 60s; a project-scoped Gemini PerDay body blocks every Gemini upstream.

License: nothing to copy. Do not port the 30-minute cap in `classify.js`.

## 3. Tool-output compression

`src/token-saver/rtk/` rewrites tool-result text before the provider sees it. User, system, and assistant text are left alone. Error results are kept. A replacement has to be non-empty and smaller. Blobs under 500 bytes or over 10 MiB pass through. A thrown filter sends the original. The diff filter keeps 100 lines per hunk, caps the result at 500 lines, and writes `... (N lines truncated)`. Their README says a live 40 KB `git diff` lost about 90% of its bytes. That was not remeasured here. Those caps make a cut of that size ordinary, and the model then answers from the truncated text.

`async_pre_call_hook` already holds the message list, so a compactor would be a new `muse/compact.py` called from that hook, default off, with bytes recorded on the call and a response header.

It pays off for agent apps that paste git, grep, and build logs (`cline`, `continue`, `aider`). It does not pay off for a long user draft, or for a file the files-answer skill read: a numbered clinical note matches their `read-numbered` and `smart-truncate` filters. A prose blob that looks like a diff would be rewritten into a different question. Their `caveman` and `ponytail` prompt families (`src/token-saver/prompts.js`) rewrite the system prompt. Leave those out.

Verdict: later. Opt-in per app. Off for privacy `private` and `local`, and off for files-answer. Every shortened blob keeps a truncation marker, and `x-muse-compact` tells the caller it happened. Size M.

Test plan: fixture diffs and grep output with a known byte ratio and a stable marker; a tool error unchanged; a user message that looks like a diff unchanged; a `private` request unchanged; a filter exception leaves the body byte-identical.

License: the comments say the filters port a Rust RTK tool and that the marker lines are the contract. This repo's MIT grant covers their JS. The Rust project's license is not in the tree. Reimplement from the idea. Do not paste `src/token-saver/rtk/`.

## 4. Other ideas, and what to refuse

Skip a hand-rolled `POST /v1/messages`. `src/forward.js` and `src/anthropic.js` translate Anthropic messages, and `count_tokens` is a local estimate with no tokenizer. The hook already handles LiteLLM's `anthropic_messages` call type for the output floor. Use the proxy's own route if a client needs that shape.

`muse dash` is already a loopback page over the ledger (tokens, latency, cost, no prompt text). Their heatmap and tokens/s are presentation. Time-to-first-token is not stored; `calls.latency_ms` is the whole call. Add charts later, size S, only if the page is hard to read. No new telemetry.

`src/effort.js` maps Light/Balanced/Deep onto 2048/8192/full capacity, and doubles the ladder when thinking cannot be turned off, because their pooled gateway ignores `reasoning_effort` and only enforces `max_tokens`. We already raise a too-small cap to `min_output_tokens` (256 on gpt-oss, measured on our keys) and set `drop_params: true`. A second ladder would fight that floor. Skip until a keyed provider documents an effort enum we currently drop and a caller depends on it. Size S, beside the floor in `muse/hook.py`.

Refuse all of the following:

- Remote announcements. `src/feed.js` polls GitHub raw and jsDelivr for HTML the repo owner pushes, including an urgent full-screen modal.
- Self-updating code. `src/updater.js` downloads a manifest, replaces package files, and hot-reloads.
- Bundled binaries. `src/engine-parts/win32-x64.*.part` and the magpie engine.
- A plugin center that installs remote extension code, signed or not.
- Client impersonation. `src/o2a/` injects an official prompt and tool list, and `src/upstream.js` sends a vendor User-Agent, so a keyless lane will answer.
- An egress proxy whose purpose is to clear an IP quota.
- `src/local-optimize.js`, canned answers for another product's quota, title, and filepath prompts.
- The account-bench cooldowns in `src/route/classify.js`.
- Either npm package. Their README says the unscoped `dsh-our-free-model` package is a third-party repack with files this tree does not contain.

No product code changes in this note. No lines from their tree are copied.
