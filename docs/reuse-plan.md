# Reuse plan for the personal gateway branch

Branch: `ccr-645e0264-bn082z` (commit `6aefbb8`).
Written 2026-11-13 (Asia/Taipei). This is a plan, not a code change.

The routing pipe should stay [LiteLLM](https://github.com/BerriAI/litellm) (MIT for the open core; the `litellm/enterprise/` tree is commercial). Muse should keep only the thin personal layer LiteLLM does not provide: a dated catalogue, usage ledger, privacy classes, per-app rationing, a daily paid cap, leftover-quota jobs, and a loopback dashboard. It should not try to out-catalogue [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (MIT), and it must stay ToS-clean.

Companion documents: [docs/plan/novelty-map.md](plan/novelty-map.md) (what in the v0.2 design is actually new) and [docs/reviews/CLAUDE-SLIMMING-PROMPT.md](reviews/CLAUDE-SLIMMING-PROMPT.md).

## Standing ToS-clean rules

These apply to every change on this branch:

1. **Official APIs only.** Use a provider's documented API, OAuth, or quota endpoint. No reverse-engineered chatbot wrappers, no unofficial "free GPT" relays.
2. **One account per provider**, belonging to the person who runs Muse. Do not store, rotate, or load-balance multiple keys for the same provider in order to multiply a free tier.
3. **No subscription proxy.** Consumer ChatGPT / Claude / Gemini / similar subscriptions are hand-off only (`config/resources/subscriptions.yaml`). Muse must never sign in to them, hold their tokens, or route API traffic through a Pro/Max/Plus plan. That pattern is what [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) and [sub2api](https://github.com/Wei-Shaw/sub2api) do, and Anthropic's Usage Policy (effective 2026-11-12, [text](https://www.anthropic.com/legal/aup)) bans reselling, proxying, or routing requests through consumer subscriptions.
4. **No key pooling across people.** One person's keys, on that person's machine or VM. No "family pool" that shares a single provider account, and no multi-tenant key redistribution in the style of [new-api](https://github.com/QuantumNous/new-api).
5. **No auto-signup / auto-registration.** `muse connect` may *guide* (open a docs URL, run a documented OAuth PKCE loopback, accept a pasted key into the local key store). It must not fill forms, click through a provider's website, or farm new accounts. A catalogue scanner may be read-only: alert that a page changed; never apply.

Free tiers are for personal and prototype use, not a production substrate. If a real product is built on top, paid or contractually private APIs replace the free path.

## Catalogue sync

`config/providers.yaml` and `config/resources/*.yaml` stay the source of truth the proxy, CLI and dashboard read. Facts in those files must be dated (`checked_on`) and must cite the provider's own page (`source_urls`). Do not fill a limit from memory.

Refresh process (next step, not in this PR):

1. Start from official pricing / rate-limit pages linked in `source_urls`.
2. Use [mnfst/awesome-free-llm-apis](https://github.com/mnfst/awesome-free-llm-apis) (CC0-1.0) only as a *lead list* of names to re-check. It is not a fork of the deleted [cheahjs/free-llm-api-resources](https://github.com/cheahjs/free-llm-api-resources) (404 as of 2026-11-13; last public Wayback snapshot on 2026-08-06 had 29,355 stars) and its inclusion rules may be looser.
3. A periodic job may fetch those official pages and flag a diff. A human (or the operator) accepts the edit. The job never signs up for a service.
4. When LiteLLM is missing a Retry-After or per-deployment cooldown behaviour we need, open an upstream issue or PR against [BerriAI/litellm](https://github.com/BerriAI/litellm) rather than growing a second router. Our `scripts/render_config.py` still writes `cooldown_time: 30`, which prevents LiteLLM 1.108.5's native Retry-After from applying. Enable the native behaviour first, then keep `muse/quota.py` only for what LiteLLM still does not do (daily/monthly quotas, cross-alias blocks, provider-specific reset clocks).

## Package boundary

This package is named `muse-stack` / import `muse`. PR #2 (`feat/phase-0-four-layers`) also uses the import name `muse`. They must not be installed in the same Python environment. Deploy them as separate compose projects. Do not merge the trees in this slimming pass.

## Module verdicts

Verdicts are **delete**, **replace with an existing project**, or **keep and strengthen**. "Replace" means stop re-implementing that job and use the named project; it does not mean copy their ToS-risky features.

### Root and packaging

| Path | Verdict | Reason | Next step |
| --- | --- | --- | --- |
| `README.md`, `pyproject.toml`, `.env.example`, `.gitignore` | Keep and strengthen | Accurate description of a *personal* official-API manager. | Mention the ToS rules and the LiteLLM dependency clearly. Keep optional extras (`keyring`, `trust`) as-is. Do not rename the import in this pass (coordinate with PR #2 first). |
| `.github/workflows/ci.yml` | Keep and strengthen | CI is required. | Upgrade deprecated Node 20 actions when convenient. Keep tests required and green. |

### `config/`

| Path | Verdict | Reason | Next step |
| --- | --- | --- | --- |
| `config/providers.yaml` | Keep and strengthen | This is Muse's dated, source-cited catalogue. Do not replace it with FreeLLMAPI's signed feed (that product is a close overlap and a paid live catalog). | Start the official-page + mnfst lead-list sync described above. Drop or retarget models that providers have scheduled to retire (see design plan v0.2 §7.1). Never add a provider without an official docs URL. |
| `config/routing.yaml` | Keep and strengthen | Task-to-alias rules the user can edit. [RouteLLM](https://github.com/lm-sys/RouteLLM) (Apache-2.0) is an inactive 2024 research router, not a replacement. | Keep the "broken file never stops the proxy" behaviour. Do not auto-add models. |
| `config/resources/chain.yaml`, `compute.yaml`, `storage.yaml` | Keep and strengthen | Read-only tracker of non-AI allowances (`managed: false`). This is the "dashboard of expiry and leftover quota" idea, and it is not something LiteLLM does. | Sync facts from official pages the same way as providers. Do not start *managing* (spending or opening) these resources automatically. |
| `config/resources/subscriptions.yaml` | Keep (do not expand into a proxy) | Already hand-off only, which is the ToS-clean design. | Add a test or comment that fails CI if a subscription ever gains a routable key or `handoff_only: false`. Do not implement CLIProxyAPI-style subscription-to-API. |

### `muse/` core

| Path | Verdict | Reason | Next step |
| --- | --- | --- | --- |
| `muse/__init__.py`, `__main__.py`, `types.py`, `paths.py` | Keep | Package surface and `$MUSE_HOME` layout. | No change unless the import-name split with PR #2 is scheduled. |
| `muse/apps.py` | Keep and strengthen | Per-application tokens and priorities so several local programs share one person's quotas. LiteLLM virtual keys are team/billing oriented; [new-api](https://github.com/QuantumNous/new-api) (AGPL-3.0) is a multi-tenant relay and the wrong model. | Keep hashing tokens (never store the raw value). |
| `muse/auth.py` | Keep and strengthen | Custom LiteLLM `user_api_key_auth` that maps a request to an app. This is glue, not a new gateway. | Keep refusing to echo secrets. Do not add a public multi-user login. |
| `muse/cache.py` | Keep and strengthen | Exact-repeat cache that respects privacy class and is keyed on the alias, which LiteLLM's built-in cache (in 1.108.5) does not do. | Leave off unless `MUSE_CACHE=exact`. Do not cache `private` / raised classes to disk. |
| `muse/catalog.py` | Keep and strengthen | Typed catalogue everyone reads. | Add a read-only sync helper (official pages + mnfst leads). Reject secret-shaped strings in YAML. |
| `muse/cli.py` | Keep and strengthen | `muse status` / `connect` / `up` is the product surface. | Do not add commands that sign up for providers or start a public server. |
| `muse/connect.py` | Keep and strengthen | Guided connect: documented OpenRouter OAuth PKCE on loopback, or a hidden paste into the key store. It already says Muse never fills, clicks, or signs in on a provider site, and never takes a key from argv. | Keep that promise. No auto-registration. No "scan and apply". |
| `muse/cost.py` | Keep | Daily paid cap must not bill a free-tier Gemini call at list price. LiteLLM's built-in price map is the fallback, not the authority for free tiers. | Keep catalogue `price:` as the cap source when present. |
| `muse/hook.py` | Keep and strengthen | The LiteLLM callback that enforces privacy, cross-alias blocks, rationing and the paid cap. This is the main reason the `muse` package exists on top of the proxy. | After enabling native Retry-After in `render_config.py`, slim any cooldown logic that now duplicates LiteLLM, but keep cross-alias and daily-quota behaviour. Contribute remaining gaps upstream. |
| `muse/keys.py` | Keep and strengthen | Keys never go in argv, logs or snapshots. Lookup order (env, keychain, dotenv) is right. | Keep. Never print values. |
| `muse/ledger.py` | Keep and strengthen | Process-safe SQLite usage ledger with no prompt text. FreeLLMAPI and others have ledgers; this one is wired to Muse rationing and the CLI. | Keep journal_mode compatible with a Docker bind mount. |
| `muse/local.py` | Keep | Discovers Ollama / LM Studio / llama.cpp on loopback and writes `local.yaml`. Not a replacement for those runtimes. | Keep the "dial loopback only" rule. |
| `muse/policy.py` | Keep and strengthen | Privacy classes (`public` / `standard` / `private` / `local`) plus a raise-only detector. Hosted cousins exist ([Vercel AI Gateway disallow-prompt-training / ZDR](https://vercel.com/docs/ai-gateway/security-and-compliance/disallow-prompt-training)); this is the self-hosted, catalogue-dated, personal version. | Keep "unknown trains = not private". Require a review date for `private`. |
| `muse/presets.py` | Keep | Data-only app presets and documented setup hints. | Keep `verified` / `UNVERIFIED` dates honest. |
| `muse/probes.py` | Keep and strengthen | Live remaining-quota probes only where the provider documents an endpoint (today: OpenRouter `GET /api/v1/key`). | Add a probe only from official docs. Never scrape a consumer account page. |
| `muse/queue.py` | Keep and strengthen | Background jobs that run on leftover allowance. This is the hook for later Lean useful-work (see novelty map), not a job for FreeLLMAPI. | Keep prompts out of the usage ledger and erase them when a job finishes. Do not send health/C4 content through this queue. |
| `muse/quota.py` | Keep and strengthen, then shrink against LiteLLM | Pure 429 / header parsing, daily/monthly quotas, Gemini Pacific midnight, long Retry-After as exhaustion. LiteLLM already cools a *single deployment* when Retry-After is enabled; it does not track daily caps or share a block across aliases. | Enable native Retry-After; keep this module for the rest; send reusable parsing upstream. |
| `muse/ration.py` | Keep and strengthen | Rations free quota and the paid cap across one person's apps (interactive first, no surprise paid fallback). This is not something to replace with a commercial gateway budget. | Keep table-tested pure functions. |
| `muse/rules.py` | Keep and strengthen | User-editable task routing. | No automatic model picker that bypasses privacy class. |
| `muse/status.py` | Keep and strengthen | One snapshot for the CLI and the dashboard. | Never include keys or prompt text. |

### `muse/dashboard/`

| Path | Verdict | Reason | Next step |
| --- | --- | --- | --- |
| `muse/dashboard/` (`server.py`, `index.html`, `demo_snapshot.json`) | Keep and strengthen | Read-only loopback UI (Host-header check, CSP, no write routes). Do not replace it with FreeLLMAPI's desktop app or a public status page that leaks inventory. | Keep binding `127.0.0.1`. A later authenticated HTTPS status page is a separate, explicit decision. |

### `muse/trust/`

| Path | Verdict | Reason | Next step |
| --- | --- | --- | --- |
| `muse/trust/` (`vault.py`, `crypto.py`, `mandates.py`, `receipts.py`, `llm.py`, `contracts.py`) | Keep, but **park**. Ask before deleting or expanding. | Local encrypted vault, mandates and hash-chained receipts for a health pack. Health spec v0.2 requires client-side decryption (WebCrypto) and passkey confirmation for sensitive grants; this tree still assumes a passphrase unlock in the process that serves the app. It must not be deployed on a public VM or used with real patient data. | Do not add features here in the slimming pass. Do not "fix" it toward a hospital product on this branch. If a later health branch supersedes it, ask first, then move or replace with the v0.2 design. Crypto primitives can stay as a library. |

### `scripts/`, compose, deploy, tests, docs

| Path | Verdict | Reason | Next step |
| --- | --- | --- | --- |
| `scripts/render_config.py`, `render_and_start.py`, `entrypoint.sh` | Keep and strengthen | Renders the LiteLLM config from the catalogue and env. | Change the hardcoded `cooldown_time: 30` so LiteLLM's native Retry-After wins. Add a regression test: a 429 with `Retry-After: 60` is not retried on the real path for 60s. |
| `scripts/validate_config.py` | Keep | Guards the catalogue. | Keep failing on secret-shaped values and undated facts. |
| `scripts/smoke_test.py`, `real_key_check.py` | Keep | Operator checks. | Run only with keys the operator entered locally. |
| `docker-compose.yml`, `docker-compose.spend.yml` | Keep | Official LiteLLM image plus this package. | Pin a multi-arch tag (Ampere A1 is arm64). Bind the proxy to loopback unless an explicit reverse proxy is added. |
| `deploy/oracle-a1-setup.sh`, `deploy/oracle-cloud.md` | Keep | Operator notes for the Always Free VM. | Keep secrets off the page. |
| `tests/` | Keep and strengthen | The safety net for slimming. | Fix `tests/test_trust_receipts.py::test_concurrent_appends_two_processes`: do not close stdin and then call `communicate()`; pass the passphrase with `communicate(input=...)`. Keep the suite green after every slimming commit. |
| `docs/muse-plan.md`, `docs/plan-infra-first.md`, `docs/real-key-checklist.md`, `docs/mockups/` | Keep | Product narrative and UX mockups. | Reconcile later with this reuse plan and `docs/plan/novelty-map.md`. Do not delete in the slimming pass. |
| `docs/reuse-plan.md`, `docs/plan/novelty-map.md`, `docs/reviews/CLAUDE-SLIMMING-PROMPT.md` | Keep | This PR. | Update if a later slimming PR changes a verdict. |

## What not to add

- A second OpenAI-compatible router (Bifrost is a possible *later* LiteLLM alternative, Apache-2.0, https://github.com/maximhq/bifrost — not a task for this pass).
- FreeLLMAPI, one-api, new-api, or Portkey as a drop-in replacement of this tree.
- New providers discovered only from a third-party list, with no official page.
- Auto-registration, multi-account key rotation, or subscription-to-API bridges.
- Health records, C4 data, or a public bind of the proxy or dashboard.
- Merging this package with PR #2 in the same Python env.

## Suggested slimming order

1. Enable LiteLLM native Retry-After in `scripts/render_config.py` and add the zero-retry test.
2. Fix `test_concurrent_appends_two_processes`.
3. Add the catalogue sync *reader* (diff against official pages + mnfst leads); no writers that apply.
4. Slim hook/quota overlap only after (1) is proven.
5. Leave `muse/trust/` parked unless the owner asks to move it.
