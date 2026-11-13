# muse-stack

Self-hosted OpenAI-compatible gateway for [Muse AIMedics](https://github.com/zandis/muse-stack). One LiteLLM proxy load-balances and falls back across free and cheap LLM, embedding, and rerank APIs. Clients call aliases (`fast`, `smart`, `cheap`, `long-context`, `embed`, `rerank`). The router picks a deployment that has a key, and on a rate limit or error it tries the next one.

The first host is a small Linux box with Docker. The later host is an Oracle Cloud Always Free ARM VM in US West (San Jose). See [deploy/oracle-cloud.md](deploy/oracle-cloud.md).

## Resource manager

`muse` is a command-line tool and a local dashboard for one person's own AI
resources: your API keys, your free-tier quotas, and models running on your
machine. It sits on top of the proxy and adds what the proxy lacks.

It is a quota-aware router for your own accounts. It does not pool accounts,
share keys, rotate identities, or use consumer-subscription logins, and it
stops calling a provider that says stop.

```bash
pip install -e .            # add ".[keyring]" to store keys in the OS keychain
muse keys set GEMINI_API_KEY   # prompts; a key is never taken from the command line
muse local discover --write    # find Ollama / LM Studio / llama.cpp models, if any
muse up                        # start the proxy (Docker). `muse up --native` if litellm is installed
                               # (install it with: pip install --require-hashes -r requirements/proxy.lock)
muse status                    # providers, quotas, reset countdowns, recent calls
muse dash                      # the same as a page on http://127.0.0.1:4180
muse ask "hello" --alias fast --privacy local --show-route
muse doctor                    # what is misconfigured and how to fix it
```

What it adds:

- **A shared block list.** When a provider says a quota is spent until
  tomorrow, every alias that uses that model skips it until the reset, across
  proxy workers and restarts. (The proxy on its own cools each alias
  separately and re-hits the same spent quota.) A rejected key pauses that
  provider for 15 minutes instead of being retried.
- **Privacy classes.** Send `x-muse-privacy: public | standard | private |
  local`, or set `MUSE_DEFAULT_PRIVACY`. `local` only reaches models on this
  machine. `private` also allows a provider whose `policy` in
  `config/providers.yaml` says, after someone has read its terms, that it does
  not train on or review prompts. If nothing qualifies the request is refused
  with HTTP 403. It never falls back to a less private provider. With
  `MUSE_AUTO_PRIVACY=on`, a prompt that contains an API key, a national ID
  number or clinical-document markers is raised to `private`; this detector is
  a safety net with real gaps, not a guarantee.
- **A usage ledger** in `~/.muse/ledger.sqlite`: which deployment answered,
  status, tokens, latency and privacy class. No prompt or response text and no
  key is ever stored.
- **Quota and reset tracking** from provider rate-limit headers and 429
  responses. `muse status --probe` also asks OpenRouter, the one provider with
  a quota endpoint. Where a provider reports nothing (Gemini), limits show as
  unknown until a 429 says otherwise.
- **Local models as first-class deployments.** Set `OLLAMA_API_BASE`,
  `LM_STUDIO_API_BASE` or `MUSE_LOCAL_OPENAI_BASE`. They join the aliases at
  `MUSE_LOCAL_ORDER` (default 5: after the free cloud tiers; 0 tries local
  first).

Response headers `x-muse-provider`, `x-muse-deployment` and `x-muse-privacy`
say where a request went. Set `MUSE_DISABLE_HOOK=true` to run the plain proxy.

**Output floor for reasoning models.** A reasoning model (the gpt-oss family, for
one) spends output tokens on hidden reasoning before it writes anything, so a
request with a small `max_tokens` comes back as HTTP 200 with empty content and
`finish_reason: "length"`. A chat deployment can carry `min_output_tokens` in
`config/providers.yaml` (per deployment, or on the provider as the default for
its chat deployments; rendered as `model_info.muse_min_output_tokens`). Two
response headers go with it:

- `x-muse-min-output: raised:<from>-><to>`, for example `raised:16->256`. The
  request set `max_tokens`, `max_completion_tokens` or `max_output_tokens`
  below the floor of the deployment that answered, and Muse sent `<to>` to that
  provider instead. `<from>` is the smallest cap the request carried. When a
  task's ceiling (`x-muse-task`) is itself below the floor, the floor wins for
  that deployment, because an empty answer is worse than a slightly longer one,
  and the value ends `;over-ceiling:<ceiling>`, for example
  `raised:100->256;over-ceiling:100`. The header is absent when nothing was
  changed: a cap at or above the floor is never lowered, a request with no cap
  gets none, and embedding and rerank calls are not touched. After a fallback
  it describes the deployment that answered; a deployment without a floor
  receives the value the client sent. For a paid deployment the daily paid cap
  is held on the raised figure.
- `x-muse-answer: empty-reasoning`. A 200 whose answer has no visible text
  and no tool call, and which either stopped at the length limit or reports
  reasoning tokens: the model used its whole allowance thinking. Ask again
  with a longer reply allowed (`muse ask` prints this hint). The body is passed
  on unchanged and nothing from it is logged or stored. Streamed responses do
  not carry this header, because headers are sent before the answer is known.

The shipped floor is 256 on the gpt-oss deployments. That figure is the
owner's observation on real keys on 2026-11-12/13 (empty at 16 to 20 tokens,
fine at 300), not a number from any provider's documentation.

A provider's `Retry-After` on a 429 is honoured twice over. LiteLLM cools that
one deployment for the stated time (the rendered config sets no `allowed_fails`
and no per-deployment `cooldown_time`, either of which would defeat this). The
hook adds what LiteLLM does not do: the same block for every alias and worker
that shares the upstream, kept across restarts, plus daily and monthly quotas.
`MUSE_COOLDOWN_FALLBACK_S` (default 30) is only used for a failure that carries
no `Retry-After`.

Known limits: calling a deployment by its id skips the block list (privacy
still applies); server errors are left to the proxy's own per-alias cooldown;
the 429 formats for Gemini, OpenRouter and Cohere are parsed from community
reports, not official documentation, and are marked `unconfirmed` in
`muse/quota.py`.

### Keeping the catalogue honest

Every fact in `config/providers.yaml` and `config/resources/*.yaml` is dated (`checked_on`) and cites the provider's own page (`source_urls`). `muse catalog check` only reads:

```bash
muse catalog check            # which entries were checked more than 30 days ago (no network)
muse catalog check --pages    # fetch each source page once; say which ones changed since the last check
muse catalog check --leads    # names on a public lead list that the catalogue lacks (names to look up, not facts)
```

It never edits the catalogue, never adds a provider, never signs up for anything and never sends a key. A "changed" page means a person should re-read it and edit the entry by hand. Page fingerprints are kept in `~/.muse/catalog-pages.json`. The rules the catalogue check enforces: official API hosts only, one entry and one key per provider, and consumer subscriptions (ChatGPT, Claude, Gemini apps) are hand-off only and can never be an API route.

### Connecting helpers

`muse connect` adds one helper at a time. Muse only guides you: it never fills in, clicks or signs in on a provider's website, and it never takes a key on the command line.

```bash
muse connect                 # what is connected, then the next helpers ranked by allowance gained per minute
muse connect openrouter      # one tap: approve on OpenRouter's page and the key comes straight back, nothing to paste
muse connect gemini          # steps from the catalogue, then a hidden prompt for the key; add --test for ONE key-check request
muse connect local           # find Ollama / LM Studio models (same discovery as `muse local discover --write`)
muse resources               # storage, compute and chain allowances, and your subscriptions: shown, not managed
```

- The one-tap flow is OAuth with PKCE (S256). Muse listens on `127.0.0.1` for one redirect on a random port and a random secret path, checks the `state` and `Host`, and shuts the listener down at once. `--no-browser` prints the link; `--paste-code` skips the listener (approve anywhere, paste the code OpenRouter shows). OpenRouter's documentation allows `http://localhost:<any port>` callbacks, so that form is used; `127.0.0.1` is not mentioned there, so it is not sent.
- `--test` sends one `GET` that checks the key (a model list or a similar endpoint) with no prompt text. Without `--test` nothing is sent. A helper with no confirmed check endpoint says so and sends nothing.
- After a key is saved, restart the proxy so it reads it: `muse restart` (Docker), or `muse restart --native --detach` when you started it with `muse up --native --detach`. Start it with `muse up` if it is not running.
- Language: `--lang en|zh-TW`, else `MUSE_LANG`, else Traditional Chinese when the system locale is Chinese.
- A suggestion quotes a gain ("adds roughly 50 requests a day") only where `config/providers.yaml` holds a number read on the provider's own page; otherwise it says "allowance not confirmed". Every catalogue fact has a `checked_on` date, a `confidence` and `source_urls`; `scripts/validate_config.py` checks the schema, including `config/resources/*.yaml` (non-AI allowances, `managed: false`) and the hand-off-only subscriptions.

### Applications, paid cap and queue

Each program that uses the proxy can have its own key, so you can see what each one uses and ration free quota between them.

```bash
muse apps preset cline --for cline        # or open-webui, librechat, continue, aider, n8n, script
muse apps add notes --priority background --daily-requests 300
muse apps list                            # today's use, caps and what was turned away
muse apps set notes --daily-tokens 500000 # also: --priority, --allow-paid / --no-paid, --privacy, --note
muse apps rotate notes                    # new key, the old one stops working
muse apps rm notes
```

A key (`sk-muse-app-...`) is shown once, with the base URL and key to paste into the tool. Muse keeps only a hash. To keep keys out of logs, `muse apps add|preset|rotate` print one only when stdout is a terminal, or when you pass `--show-token`; otherwise they refuse before changing anything. `preset` prints each tool's own setup steps and marks any it could not confirm in the tool's documentation as UNVERIFIED.

The rules, in plain words:

- **Interactive** apps (a chat window you are typing in) are never held back. **Normal** apps keep a small reserve of each allowance free for interactive ones. **Background** apps step aside while an interactive app is active (`MUSE_ACTIVE_WINDOW_S`, default 2 minutes) and keep a larger reserve, and they also wait while an allowance is being used faster than its even pace (see "Routing rules and pacing" below), except in the last hour before a daily allowance resets, when they may use what would be wasted.
- A per-app daily cap (requests or tokens) ends that app's day at the cap until local midnight.
- Paid providers are used only by apps allowed to, only while today's paid spend is under `MUSE_DAILY_PAID_CAP_USD` (default 1.00; 0 turns paid use off), and only when the price is known.
- A request that is turned away gets HTTP 429 with `x-muse-ration: defer:<reason>` (try again later; nothing was sent upstream) or `refuse:<reason>` (not today), plus `Retry-After`. A 429 without that header comes from a provider.
- A provider's 429 is passed on at once, with the same status, and says when to come back: `x-muse-wait: provider-limit` plus `Retry-After`, the seconds (at least 1) until the quota block that 429 set, or met, ends. The same pair is sent when every allowed deployment was already blocked by a provider quota, with the soonest reset. It is a separate header on purpose: `x-muse-ration` keeps meaning "Muse turned this away". The pair is never sent with a privacy refusal, never when the request ended in some other error, and never on a 200 that a fallback produced. On the plain proxy (`MUSE_DISABLE_HOOK=true`) there is no such header.

The background queue runs non-urgent jobs on allowance that would otherwise be wasted, without getting in the way of what you are doing:

```bash
muse queue add "Summarise the attached notes" --alias cheap --by 08:00   # "-" reads the prompt from stdin
muse queue add-file notes.txt --do "summarise"      # a folder queues one job per file (at most 50)
muse queue add-batch prompts.txt                    # several prompts, separated by a line of ---, one job
muse queue run                                      # Ctrl-C to stop; --once, --until-idle, --max-jobs N
muse queue list ; muse queue show ID --output ; muse queue cancel ID ; muse queue clear --done
muse pause ; muse resume                            # stops and restarts the queue; the proxy keeps working
```

Jobs live in `~/.muse/queue.sqlite` (separate from the usage ledger because they hold your prompts; a finished job's prompt is erased). Results are written to `~/.muse/queue-out/` or the path you give, never to the ledger or a log. The runner is a background app named `queue` that Muse creates and keeps (its key is stored in the key store and never printed). It does what the proxy says: a deferral is retried after `Retry-After` plus a little random delay, an app cap holds all queue jobs until midnight, a provider 429 is retried at its `Retry-After` when the proxy marks it `x-muse-wait: provider-limit` and otherwise backs off exponentially like a server error, a privacy refusal is final, and a job past its `--by` deadline fails instead of paying. The queue is free-only unless a job is added with `--allow-paid`, which uses a second managed app (`queue-paid`) that is still bound by the daily paid cap. `muse status` and the dashboard show the apps, the paid total and the queue.

To check the real providers with your own keys, see [docs/real-key-checklist.md](docs/real-key-checklist.md).

### Routing rules and pacing

**Fit the task.** `config/routing.yaml` says how strong a model each kind of task gets: `light` (classify, extract, tag) uses `fast` and at most 300 output tokens, `everyday` uses `cheap`, `heavy` uses `smart` (and `long-context` for inputs over about 60,000 tokens), `frontier` uses `smart` and suggests a hand-off, plus `embed` and `rerank`. Edit the file and the next request uses it, no restart; a broken file never stops the proxy (the built-in rules, identical to the shipped file, are used and the problem is logged once).

```bash
muse rules                     # the rules as sentences (--lang zh for Traditional Chinese, --json)
muse rules check               # validate the file; warns about aliases missing from the catalogue
muse ask --task light "Tag this: invoice from ACME"
muse queue add --task heavy "Write the weekly report"   # without --task or --alias: the queue's default task (light)
```

An app asks for a task with the header `x-muse-task: <task>` and any model name, or with the model `auto` (no header: the app's default task from the file, else `everyday`). The proxy replaces the model with the task's alias and lowers `max_tokens` to the task's ceiling (a lower one is kept). The one exception is a deployment whose output floor is above the ceiling: see `x-muse-min-output` above. An unknown task is HTTP 400 listing the valid ones; the response says `x-muse-task: <task>`. A word in a task *name* can pick the task (`classify-emails` is `light`); prompt text is never read for this.

**Spend what expires soonest.** For each daily or monthly allowance with a known limit (from the provider's headers, or the catalogue's `quota:` minus the ledger) the router draws a straight line from full at the last reset (midnight Pacific for Gemini, UTC otherwise, the calendar month, or the reset time minus one window for rolling limits) to empty at the next. `surplus = remaining/limit - (1 - elapsed/window)`.

- Background apps may use an allowance only when its surplus is at least `MUSE_PACE_MARGIN` (default 0: on or under the line) and the reserve allows it; in the last hour before a daily reset (last day before a monthly one) both are lifted. A held request gets `x-muse-ration: defer:pace` and a `Retry-After` for when the line catches up. Normal apps keep only the reserve; interactive apps are never held. Minute limits and windows of unknown length are not paced (the reserve still applies). `MUSE_PACE_MARGIN=-1` turns pacing off.
- When two free upstreams share an order level, the one that will reset with the most left over is preferred: by the projected leftover at reset when every one has a recent burn rate (at least `MUSE_PACE_BURN_MIN_CALLS`, default 3, calls in the last `MUSE_PACE_BURN_WINDOW_S`, default 3600 s), else by surplus; an upstream that reports nothing counts as on the line. The others are set aside only when they are more than `MUSE_PACE_PREFER_GAP` (default 0.10 of the allowance) behind; closer than that, requests are load-balanced as before. A retry or fallback is never narrowed. The response then says `x-muse-ration: pace:prefer`.

**Offer the hand-off.** A `frontier` task, or a refusal for the strong alias, carries `x-muse-handoff: suggest`. For a frontier task paid providers are not used until the user chooses to pay (`x-muse-allow-paid: true`), so the user first sees the choices. A refusal's message ends with the choices available now, for a bot to turn into buttons: ` | options=wait:2026-11-13T00:00:00Z,handoff,pay:0.041000` (`wait` = the soonest reset, `handoff` = open your own ChatGPT, Claude or Gemini app, `pay` = estimated US$ when this app may pay, the cap has room and a priced paid model exists; `options=none` if nothing is). Nothing is ever routed to a subscription.

**Hold one-offs.** Trial credits that never renew (marked one-off in the catalogue, rendered as `model_info.muse_oneoff`) are used only when the request says `x-muse-allow-oneoff: true`; otherwise `refuse:oneoff-held`, even when nothing else is left.

**Exact repeats** (off unless `MUSE_CACHE=exact`): an identical request (same alias, messages and output settings, temperature 0 or a task marked `deterministic`) is answered from a cache without contacting anyone (`x-muse-cache: hit`, `x-muse-provider: cache`), recorded in the ledger as `cached` at zero cost, and not counted against quotas or app caps. The privacy class and the app's default class are part of the key. Public and standard answers are kept in `~/.muse/cache.sqlite` (0600) so every worker shares them; private and local answers are kept in memory only and never written to disk. `MUSE_CACHE_TTL_S` (default 3600) and `MUSE_CACHE_MAX_ENTRIES` (default 1000) bound it.

### Home control API

The one way an app talks to the home: a small local JSON service over a private Unix socket (`~/.muse/home.sock`, mode 0600; `127.0.0.1` on Windows or with `--tcp PORT`). The contract is [docs/spec/interfaces/home-api.v1.yaml](docs/spec/interfaces/home-api.v1.yaml); a card's shape is [docs/spec/interfaces/card.schema.json](docs/spec/interfaces/card.schema.json).

```bash
muse home token add my-phone     # a device token, shown once; only its hash is kept (list, rotate, rm)
muse home serve                  # run it (Ctrl-C to stop); --tcp 4181 for 127.0.0.1
MUSE_HOME_TOKEN=... muse home call GET /v1/status          # a tiny client for debugging
python -m muse.home --fake --tcp 4181 --state cards        # canned data for building an app: no proxy, ledger or keys
```

- Every request carries `Authorization: Bearer <device token>`; on the socket the caller must also be the same user. No CORS headers are sent, bodies are bounded, and nothing from a request is logged.
- Calls, all under `/v1`: `status` (the same snapshot as `muse status --json`), `catalogue`, `connect/{provider}` (returns the steps; the client opens the pages), `keys/{provider}` (the only call that accepts a key; never echoed), `jobs` (the queue; a prompt is accepted and never returned), `cards` (a device may ask with `POST`, at most 20 waiting each, and cannot answer its own) and `cards/{id}/answer`, `pause` and `resume`, `apps` (a new app token is returned once). `receipts` and `mandates/artifacts` answer 501 until those parts exist.
- Anything key-shaped in a request to any other call is refused. Errors have one shape: `{"error": {"code", "message", "retry_after"?}}`.
- **Cards** are the questions the home asks before it acts (save a file, send a message, spend, grant a permission, use a one-off credit, send something private, install a skill). They wait in `~/.muse/cards.sqlite`, expire after 24 hours (5 minutes for a payment), and can be answered once. `muse.home.cards.CardStore` is what other code on the home uses to ask and wait.
- The fake home has six states (`normal`, `used`, `paused`, `cards`, `empty`, `error`) and signs in with the fixed token it prints. `muse dash` is unchanged and still serves its own read-only page.

### Host bots

Muse can sit underneath a bot you already run, through two doors: the bot's OpenAI-compatible engine setting (with an app key of its own) and the Muse tools server, a Model Context Protocol server the bot starts as `python -m muse.mcp`. See [docs/hosts/README.md](docs/hosts/README.md) for what Muse does and does not control there, and [docs/hosts/openmausbot.md](docs/hosts/openmausbot.md) for the first host.

```bash
muse hosts setup openmausbot       # the host's own app key (shown once), a tools token in a 0600 file, and what to paste
muse hosts show openmausbot        # print what to paste again; no secret in it
muse hosts check openmausbot ~/.openmausbot/config.json   # read-only: which engines go through Muse and which do not
muse mcp serve --token-file PATH   # the tools server on stdin/stdout (a host starts this; needs `pip install -e '.[mcp]'`)
```

- Tools: `allowance_status`, `pause_status`, `suggest_service`, `ask_approval`, `check_approval`, `check_permission` (advisory until permissions exist), `record_action` and `list_skills` / `run_skill` (answer "not built" for now), `queue_job`, `job_status`. No `pay`, `pause` or `resume`.
- The tools server talks only to the home control API with a device token; it holds no provider key. Muse controls model use, spending and its own tools; it cannot stop a host's own shell or file tools.
- No host has been installed or run with Muse yet. Host settings are dated and say where they were read.

## Trust layer

`muse/trust` holds what the bot and later data sales are built on: an encrypted
vault only the user's passphrase opens, mandates (scoped, revocable permissions
the user signs), and a signed, hash-chained receipt for every action. Install
with `pip install -e ".[trust]"`. The health assistant that first used these is
parked on the `health-assistant-pack` branch as an optional add-on.

## Do not send patient data through this

Free and trial tiers are not a place for clinical records.

- Gemini's pricing page says free-tier content **is used to improve Google's products**. Paid Gemini is the tier that is not used that way.
- Groq, OpenRouter's free variants, Cohere trial keys, and the other free endpoints log prompts on their side. Several of them train on that traffic or share it with the model host.
- The San Jose VM is in the United States. A request from Taiwan to this proxy, and from the proxy to a provider, leaves Taiwan.
- This repository is a routing layer for a side project. It is not a medical device, it does not sign a BAA with these providers, and it does not make a free tier HIPAA- or PDPA-safe.

Use it for synthetic notes, public guidelines, and code. Keep identifiable health data off it.

## How a request is routed

LiteLLM treats every `model_list` entry with the same `model_name` as one group. The public aliases are those group names.

Inside a group, `order` is the priority (lower first). Deployments with the same order are load-balanced with `simple-shuffle`, weighted by the `rpm` hint in the catalog. Those `rpm` numbers are weights, not the provider's real quota.

When a deployment returns a rate limit or a server error, the router retries once, then moves to the next order. After the group is exhausted, chat aliases fall back to each other (`fast` → `cheap` and `smart`, and the reverse). A context-window error on `fast`, `cheap`, or `smart` jumps to `long-context` instead of retrying.

OpenRouter's free models are order 9, so they are the last resort. `openrouter/free` is in that last band too: OpenRouter chooses which free model answers, and the choice can change between calls.

Novita, DeepSeek, Voyage, and NVIDIA NIM are paid or credit-metered. Setting their keys registers a **direct model name** (`deepseek-flash`, `voyage-4-lite`, `nim-gpt-oss-20b`, …) but does not put them in the public aliases. A Gemini 429 will not start a DeepSeek bill. Set `MUSE_ALLOW_PAID_FALLBACK=true` when you want those providers in the alias chain, after the free ones.

Each deployment is also exposed under its own name (`gemini-3.8-flash`, `groq-gpt-oss-120b`, …). Use that when you need a stable model rather than a routed alias.

`embed` and `rerank` fall back the same way, with one hard limit: **vectors and scores are not interchangeable across providers.** `embed` will fail over from Gemini to Jina or Cohere if Gemini is down, and those vectors have different dimensions and live in different spaces. Do not write a fallback response into an index built with another provider. Pin `gemini-embedding-2`, `jina-embeddings-v3`, `cohere-embed-v5-fast`, `siliconflow-bge-m3`, or `voyage-4-lite` for anything you store. Rerank scores from Cohere, Jina, SiliconFlow, and Voyage are not on the same scale either.

Background health checks are off. A check every few seconds would burn the free quotas.

## Setup

Docker, and a Compose v2 plugin. The proxy image is `ghcr.io/berriai/litellm:v1.108.5` (amd64 and arm64), pinned by digest in `docker-compose.yml`; see "Supply chain" below.

```bash
cp .env.example .env
printf 'LITELLM_MASTER_KEY=sk-%s\n' "$(openssl rand -hex 32)" >> .env
# fill in whichever provider keys you have; leave the rest blank

docker compose up -d
curl -fsS http://127.0.0.1:4000/health/liveliness
python3 scripts/smoke_test.py
```

The compose file publishes `127.0.0.1:4000` only. Set `MUSE_BIND=0.0.0.0` if you want the host's other interfaces, and put a TLS proxy in front before you do that on a machine with a public address. Send the master key on every client call:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"Say hello in one sentence."}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:4000/v1", api_key="YOUR_LITELLM_MASTER_KEY")
client.chat.completions.create(model="cheap", messages=[{"role": "user", "content": "hello"}])
client.embeddings.create(model="embed", input="a short phrase")
```

The admin UI is at `http://127.0.0.1:4000/ui`. The master key is the password.

LiteLLM 1.108.5 refuses to start when no master key is set. If `LITELLM_MASTER_KEY` is empty, the renderer sets LiteLLM's `dangerously_permit_weak_or_unset_master_key` so a first boot with an empty `.env` still comes up, and the proxy does not check `Authorization`. That override is only acceptable on localhost. Set a real master key (not a published example such as `sk-1234`, which this LiteLLM version also rejects) before the port is reachable from anywhere else.

Startup prints the providers it actually enabled. A missing key is omitted from the generated config (`/tmp/muse-config.yaml` inside the container). It is not an error.

To inspect the config without Docker:

```bash
python3 scripts/render_config.py --env-file .env --stdout
```

PyYAML is the only dependency of the renderer (`pip install pyyaml`). The container image already has it.

### Supply chain

LiteLLM is the one large third-party program that sees every provider key, so the repo fixes exactly which LiteLLM runs. The reason is recent: on 21 October 2026 LiteLLM's own release pipeline published a malicious `litellm==1.107.0` to PyPI and pointed the image tags `v1.107.0`, `latest`, `main-latest` and `main-stable` at a malicious image for about two hours ([incident report](https://docs.litellm.ai/blog/security-incident-oct-21-2026), [how it happened](https://docs.litellm.ai/blog/security-incident-oct-21-2026-attack-chain), [what they are changing](https://docs.litellm.ai/blog/security-changes-after-oct-21-2026)). Those artefacts were validly signed with LiteLLM's old image-signing key, so a signature alone did not help; a pinned version or digest did.

What is pinned:

- **The image, by digest.** `docker-compose.yml` uses `ghcr.io/berriai/litellm:v1.108.5@sha256:008a5916c0a0ca6f4f6ad64969f6a4ef6f03ce885a639f3d97fd53665754ce48`. That is the multi-arch index (linux/amd64 `sha256:4e613e76…`, linux/arm64 `sha256:23192ecd…`; both in full in the compose file), read from ghcr.io on 13 November 2026. Docker pulls by the digest, so moving the tag changes nothing here.
- **The Python install, by hash.** `requirements/proxy.lock` lists `litellm[proxy]==1.108.5` and all 119 packages it pulls in, each with the SHA-256 of every file PyPI holds for that version. `pip install --require-hashes -r requirements/proxy.lock` refuses anything else. `requirements/dev.lock` does the same for pytest and setuptools. The resolution is for CPython 3.12 and 3.13 on any platform, Linux x86_64 and aarch64 included; it was installed and tested on CPython 3.12 and 3.13 on Linux x86_64; for Linux aarch64 every pinned wheel was fetched with hash checking for both Pythons, but not installed or run there. PyPI received the 1.108.5 files on 10 November 2026 at 22:47 UTC, after the incident and after the key change.
- **The rules.** `scripts/validate_config.py` fails if a compose file names a LiteLLM image without an `@sha256:` digest or with a floating tag (`latest`, `main-latest`, `main-stable`, `main`), or if any tracked file installs `litellm` without an exact `==` version or the lock file.

How to check:

```bash
python scripts/verify_litellm.py            # the installed package and the incident's indicator files
python scripts/verify_litellm.py --image    # also asks ghcr.io what the tag points at (the only network use)
```

It reads no key and changes nothing; exit status 0 is clean, 1 a problem, 2 something it could not check. It compares the installed version with the lock, every installed LiteLLM file with the hash pip recorded at install, looks for `cook.pth`, `cook.py`, an unexpected `sitecustomize.py` and `/tmp/remote_code_loader.lock`, and with `--image` compares the pinned digest with the registry and with the digest LiteLLM listed as compromised. It cannot tell whether the wheel pip installed was the right one (only `--require-hashes` does that, at install time), and finding no indicator file does not prove an environment was never exposed. To inspect an environment you distrust, do not start its own Python: run the script from another interpreter with `--site-packages DIR`.

The image signature can be checked with [cosign](https://docs.sigstore.dev/cosign/). LiteLLM replaced its signing key on 23 October 2026 and says every release since 24 October is signed with the new one. The SHA-256 of the new `cosign.pub` is `e3fe3e08c44b16407fe0b37c43b4f8b603b3a0f4ee74c29e647bc022d762c2c0`:

```bash
curl -sL https://raw.githubusercontent.com/BerriAI/litellm/e82a3c646908ba0a6ee0beddc4310d7a6cac4065/cosign.pub -o cosign.pub
shasum -a 256 cosign.pub      # must print e3fe3e08…c2c0
cosign verify --key cosign.pub ghcr.io/berriai/litellm@sha256:008a5916c0a0ca6f4f6ad64969f6a4ef6f03ce885a639f3d97fd53665754ce48
```

On 13 November 2026 the key file had that fingerprint and the signature stored next to this digest on ghcr.io was valid under it (checked by hand with the public key, without the cosign program; the transparency-log entry was not checked). Never accept a signature from the old key for anything published on or after 21 October.

Upgrading LiteLLM is a deliberate change, in this order:

1. Pick a release tag (never a floating one) and read its notes. Leave a new release a few days first: the October one was caught within 20 minutes.
2. Resolve the tag's digest and put tag and digest in `docker-compose.yml`: `docker buildx imagetools inspect ghcr.io/berriai/litellm:vX.Y.Z` prints the index digest and both platform digests. Check it is not a digest from an incident report, and verify the signature as above.
3. Change the version in `requirements/proxy.in` and regenerate both lock files with the `uv pip compile` commands in their headers. Compare the litellm hashes with `https://pypi.org/pypi/litellm/X.Y.Z/json` and put the header back.
4. Change the version where prose still names it (this README, `deploy/oracle-cloud.md`, comments in `muse/`). CI and the setup script install from the lock file and need no edit.
5. Run `python scripts/validate_config.py`, `python scripts/verify_litellm.py --image` and the whole suite (`python -m pytest tests -q`) in an environment installed from the new lock. The hook relies on LiteLLM internals that are noted with the version they were read in.

One rule goes with this: **provider keys live only in the proxy's environment** (`.env` with mode 600, or the OS keychain handed over by `muse up`). Applications get their own `sk-muse-app-…` key and never a provider key; CI, the lock files, the rendered config and the image hold none. If LiteLLM is ever compromised again, the keys to rotate are the ones in that one place.

Not covered: `pip` itself and the `pip install -e .` of the plain CLI (PyYAML, and the optional extras) are not hash-locked outside the native proxy install, and `postgres:16-alpine` in `docker-compose.spend.yml` is pinned by tag only.

### Spend logs

Routing does not need a database. For spend rows and virtual keys:

```bash
# set POSTGRES_PASSWORD and LITELLM_SALT_KEY in .env first
docker compose -f docker-compose.yml -f docker-compose.spend.yml up -d
```

`LITELLM_SALT_KEY` encrypts secrets that land in Postgres. Generate it once with `openssl rand -hex 32` and do not change it. Use a hex `POSTGRES_PASSWORD` too: it is placed into `DATABASE_URL` as-is, so `@` or `:` in the password will break the URL. Models stay in the rendered file (`STORE_MODEL_IN_DB=false`).

## Aliases

| Alias | Endpoint | What it prefers |
| --- | --- | --- |
| `fast` | `/v1/chat/completions` | Groq `gpt-oss-20b`, Gemini 3.1 Flash-Lite, then Cohere Command R7B, then SiliconFlow, then OpenRouter free |
| `cheap` | `/v1/chat/completions` | Gemini Flash-Lite and Groq `gpt-oss-20b`, then the same later bands |
| `smart` | `/v1/chat/completions` | Gemini 3.8 Flash and Groq `gpt-oss-120b`, then Gemini 2.5 Pro and Cohere Command A, then OpenRouter free |
| `long-context` | `/v1/chat/completions` | Models at 200k tokens or more. Gemini 2.5 Flash and 3.8 Flash first (1,048,576). Groq's 131k models are not in this group |
| `embed` | `/v1/embeddings` | Gemini Embedding 2, then Jina, Cohere, SiliconFlow bge-m3 |
| `rerank` | `/v1/rerank` | Cohere Rerank v4 fast, then Jina v2 and SiliconFlow bge-reranker-v2-m3 |

Paid deployments join those groups only when `MUSE_ALLOW_PAID_FALLBACK=true`.

## Providers, limits, and what was checked

Checked on 11 November 2026. Quotas move. When a number is not in the table, the provider does not publish a stable one, or the page that has it needs a login.

| Provider | Key | Role here | What is actually free or cheap |
| --- | --- | --- | --- |
| Gemini (AI Studio) | `GEMINI_API_KEY` | Default chat and embeddings | [Pricing](https://ai.google.dev/gemini-api/docs/pricing) lists `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.5-pro`, and `gemini-embedding-2` as free of charge on the standard tier. RPM and RPD are per project in [AI Studio](https://aistudio.google.com/); they are not pinned in this repo. Free-tier prompts are used to improve Google's products. `gemini-2.0-flash` is shut down. `gemini-4-argon` and `gemini-3.1-pro-preview` are not on the free tier. |
| Groq | `GROQ_API_KEY` | Fast and smart chat | [Models](https://console.groq.com/docs/models): `openai/gpt-oss-20b` and `openai/gpt-oss-120b`, 131,072 context. The rate column on that page is the Developer plan (250K TPM, 1K RPM). Free-plan caps are on [the limits page](https://console.groq.com/settings/limits) and were not readable without a login. `llama-3.3-70b-versatile` is enterprise-only there, so it is not configured. |
| OpenRouter | `OPENROUTER_API_KEY` | Last-resort free chat | Public [models API](https://openrouter.ai/api/v1/models) on this date, zero-priced ids only: `google/gemma-4-26b-a4b-it:free`, `google/gemma-4-31b-it:free`, `liquid/lfm-2.5-2.6b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3.5-lightning:free`, and `openrouter/free`. Free ids disappear without notice. Account rate limits are not in the catalog response. Do not add a paid id to this provider's fallbacks: OpenRouter bills it. |
| Cohere | `COHERE_API_KEY` | Trial chat, embed, rerank | [Models](https://docs.cohere.com/docs/models): `command-r7b-12-2024` (128k), `command-a-03-2025` (256k), `embed-v5.0-fast` (default 2048 dimensions), `rerank-v4.0-fast` (32k). [Trial keys](https://docs.cohere.com/docs/rate-limits) are 1,000 calls/month, and chat is about 20 RPM. Cohere's current rerank recommendation is `rerank-v5.0-fast`; it is not in LiteLLM 1.108.5's price map, so the proxy calls v4.0-fast. |
| SiliconFlow | `SILICONFLOW_API_KEY` | International endpoint, after the free direct providers | Base `https://api.siliconflow.com/v1`. LiteLLM 1.108.5 has no `siliconflow/` provider. Chat and embeddings use the `openai/` prefix with that base; rerank uses `hosted_vllm/`, which posts to `https://api.siliconflow.com/v1/rerank`. The models list returned 401 without a key. `BAAI/bge-reranker-v2-m3` is the id in their rerank docs. `BAAI/bge-m3` is the id in their FastGPT guide (1024 dimensions). **`Qwen/Qwen3.5-9B` was not verified.** The pricing page shows the display name Qwen3.5-9B; the `Qwen/` prefix is inferred. New accounts get $1 of credit. The fetched pricing HTML did not list a permanently free set. |
| Jina | `JINA_API_KEY` | Embeddings and rerank | `jina-embeddings-v3` (1024 dimensions) and `jina-reranker-v2-base-multilingual`. Both names are on jina.ai, and the reranker is in the LiteLLM 1.108.5 map. v5 embeddings and reranker v3 / v3.5 are on the site and are not wired. The current free-token allowance was not re-checked. |
| Cerebras | `CEREBRAS_API_KEY` | Free-trial chat | [Rate limits](https://inference-docs.cerebras.ai/support/rate-limits), read 12 November 2026: `gpt-oss-120b` and `qwen-3.8-27b` each get 5 requests a minute and 1M tokens a day, replenished continuously. LiteLLM 1.108.5 routes `cerebras/<model>` to `https://api.cerebras.ai/v1`. Training terms for the API were not confirmed. |
| Cloudflare Workers AI | `CLOUDFLARE_API_KEY` and `CLOUDFLARE_ACCOUNT_ID` | Free-plan chat | [Pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/): 10,000 neurons a day, reset at 00:00 UTC. Cloudflare [says](https://developers.cloudflare.com/workers-ai/platform/data-usage/) it does not train on Workers AI content. Both variables are needed; LiteLLM builds the endpoint from the account id. |
| Novita | `NOVITA_API_KEY` | Cheap paid chat, opt-in | Live list at `https://api.novita.ai/v3/openai/models`: Llama 3.1 8B at $0.02 / $0.05 per 1M (16k context), `openai/gpt-oss-120b` at $0.05 / $0.25 (131k), `qwen/qwen3.8-flash` at $0.15 / $0.47 (1M). No zero-price chat model worth routing was in that list. |
| DeepSeek | `DEEPSEEK_API_KEY` | Paid, optional | [Pricing](https://api-docs.deepseek.com/quick_start/pricing): call `deepseek-flash` and `deepseek-v4-pro`. Both are 1M context. `deepseek-chat` and `deepseek-reasoner` are retired. Flash cache-miss input is about $0.15 off-peak / $0.30 peak per 1M tokens. The proxy sets `api_base` to `https://api.deepseek.com/v1` because LiteLLM's own default is the `/beta` FIM host. |
| Voyage | `VOYAGE_API_KEY` | Paid embed and rerank, opt-in | [Embeddings](https://docs.voyageai.com/docs/embeddings): `voyage-4-lite`, 32k context, 1024 dimensions by default. [Rerankers](https://docs.voyageai.com/docs/reranker): `rerank-3-lite`, 32k. |
| NVIDIA NIM | `NVIDIA_NIM_API_KEY` | Optional credits | `GET https://integrate.api.nvidia.com/v1/models` listed `openai/gpt-oss-20b`, `nvidia/nemotron-3-ultra-550b-a55b`, and `nvidia/nemotron-3.5-embed-1b` without an API key. A listing is not a promise that a trial key can call the model, and the credit price was not checked. The embed model is **not** in the `embed` alias; its dimension was not verified. Call it as `nim-embed` if you want it. The 1M context on the ultra model is taken from OpenRouter's catalog for the same name, not from an NVIDIA page. |

Keys and the page to create them are listed in [.env.example](.env.example).

### Not verified, on purpose

- SiliconFlow chat model id `Qwen/Qwen3.5-9B` (display name only).
- Whether SiliconFlow still serves BGE models at no charge.
- Groq free-plan RPM, TPM, and RPD (console login).
- Numeric Gemini free-tier RPM/RPD (shown in AI Studio per project).
- Output dimension of `gemini-embedding-2`.
- Jina's current free quota.
- Which NIM models a fresh trial key can actually call.
- OpenRouter per-account free rate limits.
- Cohere `rerank-v5.0-fast` against this LiteLLM version (not in the price map, so it is not configured).

Some Gemini and Groq models spend tokens on reasoning. That usage counts against the free quota even when the visible answer is short.

## Missing keys and turning a provider off

The container entrypoint runs `scripts/render_config.py` before LiteLLM. A provider is included only when its environment variable is non-empty. An empty `.env` still boots: the model list is empty, `/health/liveliness` returns 200, and the smoke test skips every alias.

To keep a key in `.env` but stop routing to that provider:

```bash
MUSE_DISABLE_PROVIDERS=groq,novita
```

Provider ids are the `id` fields in [config/providers.yaml](config/providers.yaml): `gemini`, `groq`, `cohere`, `openrouter`, `siliconflow`, `jina`, `novita`, `deepseek`, `voyage`, `nvidia_nim`, `cerebras`, `cloudflare`.

## Speech

Deepgram, ElevenLabs, and AssemblyAI are not in the proxy. LiteLLM's audio coverage for those three is uneven, and a half-configured route is worse than a direct call. Their keys are in `.env.example` so they live next to the others. Point a client at the provider:

| Provider | Key | API |
| --- | --- | --- |
| Deepgram (speech-to-text) | `DEEPGRAM_API_KEY` from https://console.deepgram.com/signup | `https://api.deepgram.com` |
| ElevenLabs (text-to-speech) | `ELEVENLABS_API_KEY` from https://elevenlabs.io/app/settings/api-keys | `https://api.elevenlabs.io` |
| AssemblyAI (speech-to-text) | `ASSEMBLYAI_API_KEY` from https://www.assemblyai.com/app/account | `https://api.assemblyai.com` |

The same patient-data rule applies. These vendors are not covered by this proxy's master key.

## Add a provider

1. Add a block under `providers` in `config/providers.yaml`. Set `env`, `key_url`, and one or more deployments. `join: always` puts it in the aliases whenever the key is set. `join: paid_fallback` keeps it as a direct model name until `MUSE_ALLOW_PAID_FALLBACK=true`.
2. Put the variable and the signup URL in `.env.example`.
3. For an OpenAI-compatible host that LiteLLM does not know, use `model: openai/<their-model-id>` and set `api_base`. For rerank on that kind of host, `hosted_vllm/<model-id>` posts to `{api_base}/rerank`. SiliconFlow is the example in the catalog.
4. Run `python3 scripts/validate_config.py`. It renders the catalog with no keys, with one key, and with the paid-fallback flag, and it checks that no secret value is written into the YAML.
5. Restart the container. Startup re-reads the catalog.

Give a chat deployment `context_tokens` of at least 200000 before adding it to `long-context`. The validator rejects smaller ones. Do not add an embedding model to `embed` unless you are willing to document its dimension, and prefer a direct name if the dimension differs from the model you store.

## Smoke test

`scripts/smoke_test.py` checks `/health/liveliness` and `/v1/models`, then calls each alias that is actually configured. Anything absent is a skip, not a failure. `--strict` makes a missing alias fail.

```bash
python3 scripts/smoke_test.py
python3 scripts/smoke_test.py --base-url http://127.0.0.1:4000 --strict
```

It reads `LITELLM_MASTER_KEY` from the environment or from `.env`. It does not create provider keys.

## CI

`.github/workflows/ci.yml` renders the config with empty and partial environments and does not call a provider. The proxy tests install LiteLLM from `requirements/proxy.lock` with `--require-hashes`. A second job boots the pinned image with a throwaway master key, checks the health endpoint, and checks that a fake Gemini key exposes the chat and embed aliases without sending a completion.

## Layout

| Path | Purpose |
| --- | --- |
| `config/providers.yaml` | Catalog of providers, models, orders, and what was verified |
| `scripts/render_config.py` | Builds the LiteLLM YAML from the catalog and the environment |
| `scripts/render_and_start.py` | Render, then exec `litellm` |
| `scripts/smoke_test.py` | Health check plus one call per configured alias |
| `scripts/validate_config.py` | The CI checks |
| `muse/home/` | The home control API: server, device tokens, cards, the fake home |
| `muse/mcp/` | The Muse tools server for host bots (MCP over stdio), host settings and the read-only host check |
| `docs/spec/interfaces/` | `home-api.v1.yaml` (OpenAPI) and `card.schema.json` |
| `scripts/verify_litellm.py` | Checks the installed LiteLLM and the pinned image (read-only, no keys) |
| `requirements/proxy.lock`, `requirements/dev.lock` | Hash-locked installs: LiteLLM with its dependencies, and the test tools |
| `docker-compose.yml` | Proxy only |
| `docker-compose.spend.yml` | Optional Postgres |
| `.env.example` | Every variable and the page that issues the key |
| `deploy/oracle-cloud.md` | Always Free ARM VM in us-sanjose-1 |
