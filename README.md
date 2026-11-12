# muse-stack

Self-hosted OpenAI-compatible gateway for [Muse AIMedics](https://github.com/zandis/muse-stack). One LiteLLM proxy load-balances and falls back across free and cheap LLM, embedding, and rerank APIs. Clients call aliases (`fast`, `smart`, `cheap`, `long-context`, `embed`, `rerank`). The router picks a deployment that has a key, and on a rate limit or error it tries the next one.

The first host is a small Linux box with Docker. The later host is an Oracle Cloud Always Free ARM VM in US West (San Jose). See [deploy/oracle-cloud.md](deploy/oracle-cloud.md).

## Phase 0: four layers

This branch, stacked on the LiteLLM gateway, adds the rest of the phase 0 skeleton described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):

1. **Routing additions.** LiteLLM 1.108.5 already honours `Retry-After` (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)). Phase 0 adds a Muse-side circuit breaker for useful-work jobs, a thin LiteLLM callback only for quota-exhausted state and cross-alias cooldown, fallback chains, and `GET /v1/providers/health` (and `/usage`, `/keys`) on a small service at `127.0.0.1:4100`. The service refuses to start without a master key unless it is bound to loopback. Members still add keys via `.env`; values are never committed or returned.
2. **Results settlement.** An [x402](https://docs.cdp.coinbase.com/x402/core-concepts/how-it-works)-style HTTP 402 stub that sells a *real* verified result for USDC on **Base Sepolia**, with a **mock verifier only**. It starts only on a case-insensitive Base Sepolia allow-list, binds payments to a quote (nonce = SHA-256 of the quote id), rejects replays and expired quotes, ignores unsigned `amount` / `payTo`, and never moves funds. Mock Lean results are never sold. Job APIs never return a proof.
3. **Useful work.** A job runner that takes a Lean problem (bundled samples, or a local `formal-conjectures` checkout), asks routed aliases for a proof, and verifies it against the *trusted* statement (see [docs/LEAN.md](docs/LEAN.md)). Mock mode never emits `verified`. Real Lean runs in a sandbox (no provider keys, no network, rlimits, timeout; bubblewrap when available; gVisor / Firecracker documented for production).
4. **Registry contract skeleton.** Solidity interfaces and a `DataVaultRegistry` — a *registry* of hashes and consent, not a data vault (hash, consent, tier, hospital EIP-712 provenance with a deadline, signature hash, issuer allow-list). Foundry tests only; **no deployment**. Per-patient cohort linkage on-chain is out of scope (see [contracts/README.md](contracts/README.md)).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests -q
LITELLM_MASTER_KEY=sk-dev python -m muse    # http://127.0.0.1:4100  (mock LLM, mock Lean; master key required off loopback)
```

The LiteLLM proxy on port 4000 is unchanged; the phase 0 service is a sibling, not a replacement. There is no billing integration and no mainnet path.

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

Docker, and a Compose v2 plugin. The proxy image is `ghcr.io/berriai/litellm:v1.108.5` (amd64 and arm64).

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

Provider ids are the `id` fields in [config/providers.yaml](config/providers.yaml): `gemini`, `groq`, `cohere`, `openrouter`, `siliconflow`, `jina`, `novita`, `deepseek`, `voyage`, `nvidia_nim`.

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

`.github/workflows/ci.yml` has four jobs:

- `validate` renders the LiteLLM catalog with empty and partial environments and does not call a provider.
- `boot` starts the pinned LiteLLM image with a throwaway master key, checks `/health/liveliness`, and checks that a fake Gemini key exposes the chat and embed aliases without sending a completion. Unchanged from the gateway PR; it does not start the phase 0 service.
- `phase0-python` runs `pytest` for the circuit breaker, the x402 mock, the useful-work runner, and the phase 0 HTTP service. Lean is mocked.
- `phase0-contracts` installs Foundry and runs `forge test` in `contracts/`. No deploy, no RPC.

## Layout

| Path | Purpose |
| --- | --- |
| `config/providers.yaml` | Catalog of providers, models, orders, and what was verified |
| `scripts/render_config.py` | Builds the LiteLLM YAML from the catalog and the environment |
| `scripts/render_and_start.py` | Render, then exec `litellm` |
| `scripts/smoke_test.py` | Health check plus one call per configured alias |
| `scripts/validate_config.py` | The CI checks |
| `docker-compose.yml` | Proxy only |
| `docker-compose.spend.yml` | Optional Postgres |
| `.env.example` | Every variable and the page that issues the key |
| `deploy/oracle-cloud.md` | Always Free ARM VM in us-sanjose-1 |
| `muse/` | Phase 0 Python package: gateway, settlement, useful work, HTTP service |
| `tests/` | Pytest for phase 0 (no provider calls, no real funds) |
| `contracts/` | Data-vault Solidity skeleton and Foundry tests (no deploy) |
| `docs/ARCHITECTURE.md` | Four-layer design, what works, what is stubbed |
| `docs/LEAN.md` | Mock vs real Lean, formal-conjectures |
| `docker-compose.phase0.yml` | Optional compose for the phase 0 service |
| `requirements.txt` | Python deps of the phase 0 service |
