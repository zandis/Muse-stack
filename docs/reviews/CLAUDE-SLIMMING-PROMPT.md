# Prompt for Claude: slim the personal gateway branch

Paste everything below the line into a Claude session that has access to `zandis/Muse-stack`.

> Pass 1 of this prompt was carried out in PR #6 (merged). One instruction below was based on a wrong diagnosis and is corrected in place: the blocker for native Retry-After was the router's `allowed_fails: 2`, not `cooldown_time: 30`.

---

You are working on the private GitHub repo `zandis/Muse-stack`.

## Read this first

1. Open the pull request that adds the reuse plan (title: **Add reuse plan: what to drop, replace, keep**, branch `docs/reuse-plan`, base `ccr-645e0264-bn082z`). Read, in this order:
   - `docs/reuse-plan.md` — module-by-module verdicts (delete / replace / keep & strengthen), catalogue sync, and ToS-clean rules.
   - `docs/plan/novelty-map.md` — what is actually new versus what already exists.
2. Slim **your** personal-gateway branch (`ccr-645e0264-bn082z` and commits you add on top of it) so it follows that plan. Do not treat the reuse-plan branch as a place to rewrite product code.

## How to change code

- Work in small, reviewable commits, each with one purpose (for example: enable native Retry-After; fix one test; add a read-only catalogue-diff helper).
- After every commit, run the test suite and leave it green.
- There is a known test bug in `tests/test_trust_receipts.py::test_concurrent_appends_two_processes`: the test closes stdin and then calls `communicate()`, which tries to flush a closed stdin and raises `flush of closed file`. Fix it by passing the passphrase with `communicate(input=...)` (do not close stdin first). Do not weaken the test.
- Prefer LiteLLM for routing, retries and per-deployment cooldown. In `scripts/render_config.py`, do not set `allowed_fails` on the router (it switches LiteLLM 1.108.5 to a legacy rule that ignores a single 429) and do not set `cooldown_time` on a deployment (it overrides the header), so LiteLLM's native Retry-After can apply. The router `cooldown_time` is only the no-header fallback. Add a regression test that a 429 with `Retry-After: 60` is not retried on the real path for 60 seconds. If Muse still needs behaviour LiteLLM lacks (daily/monthly quotas, cross-alias blocks), keep that in `muse/quota.py` / `muse/hook.py` and consider an upstream LiteLLM issue or PR. Do not fork LiteLLM.
- Catalogue facts (`config/providers.yaml`, `config/resources/*.yaml`) must be dated and cited to the provider's own page. You may use https://github.com/mnfst/awesome-free-llm-apis (CC0-1.0) as a *lead list* of names to re-check, then verify on official pages. The older cheahjs/free-llm-api-resources list has been deleted (GitHub 404).

## Do not do these

- Do not add new providers in this pass, and do not add auto-registration or auto-signup of any kind. `muse connect` may guide, open a documented docs URL, run a documented OAuth PKCE loopback, or accept a pasted key into the local key store. It must not fill forms, click a provider's website, or create accounts.
- Follow the ToS-clean rules in `docs/reuse-plan.md` with no exceptions:
  - official APIs only (no reverse-engineered chatbot wrappers);
  - one account per provider, belonging to the operator;
  - no subscription proxy (ChatGPT / Claude / Gemini consumer plans stay hand-off only; never sign in, hold their tokens, or route API traffic through them);
  - no key pooling across people, and no rotating multiple keys to multiply a free tier;
  - no auto-signup.
- Do not push to `main` (or to any default branch). Open a pull request instead, and wait for review. Do not force-push. Do not merge PRs.
- Do not delete or gut a large module (`muse/trust/`, the dashboard, the catalogue, the hook, the queue, the ledger, the CLI) without asking first and waiting for a yes. `muse/trust/` is parked: do not expand it, and do not deploy it with real health data.
- Do not install this package in the same Python environment as PR #2's `muse` package (`feat/phase-0-four-layers`). They share an import name.
- Do not bind the proxy or dashboard to a public interface in this pass. Do not put secrets, `.env` values, or API keys in git, in chat, or in PR text.
- Do not add subscription-to-API bridges, FreeLLMAPI/new-api/one-api as a drop-in replacement, or health/C4 data on this branch.

## When you are done

Open a PR against `ccr-645e0264-bn082z` (or against `docs/reuse-plan` only if you are updating the plan documents). In the PR body, list what you slimmed, what you left parked, and the test command plus results.

If a reuse-plan verdict is wrong once you are in the code, say so in the PR and propose a change to `docs/reuse-plan.md` rather than silently ignoring the plan.
