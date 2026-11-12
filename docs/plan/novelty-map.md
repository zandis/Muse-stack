# Novelty map (design plan v0.2, phase 0, health spec v0.2)

Written 2026-11-13 (Asia/Taipei). Classifications:

- **(a) already done** — replace or reuse the named project; do not rebuild it.
- **(b) partially exists** — keep the idea only if the difference below is real and is actually built.
- **(c) genuinely scarce** — no public product found that combines the same pieces. This is not a claim of a patent or of "nobody has ever thought of it." Where the search was thin, the row says so.

Links were fetched or resolved on 2026-11-13. Star counts, where given, are GitHub API figures from that day and will change.

The personal-gateway branch implements only Part A routing glue (catalogue, ledger, privacy, rationing, queue). Phase 0 useful-work / x402 / DataVaultRegistry live on `feat/phase-0-four-layers` (PR #2). Health spec v0.2 is documentation on `docs/health-bot-spec-v0.2` (PR #4); the parked health pack is a different branch and is **not** v0.2-compliant yet.

## Short list

**Strongest scarce combinations (build these, after demand checks):**

1. Leftover *personal, official* LLM quota, queued when interactive work is idle, spent on machine-checked tasks (Lean first; later Dafny / Verus / sorry-fill), with the verified artifact and any bounty or signal credit accruing to the contributor — not to a token miner. Individual pieces exist (conjectures.io, LiteLLM queues, people burning their own Copilot quota on Lean). The productised leftover-quota → verified work → contributor-benefit loop was not found as a shipped system.
2. A **verification-signal** (model or model-combo success rate, attempts, cost on real verified tasks), sold or sponsored in dollars, with results published. conjectures.io pays for accepted proofs in TAO; LMSYS-style arenas rank chat quality. A vendor-funded, no-token leaderboard built from leftover personal compute is the intended difference, and it is still a demand risk (design plan §9.5: no evidence yet that labs buy third-party proof traces).
3. **Unlinkable benefit payouts** for sensitive living cohorts: all income in one pool, Merkle commitment of amounts, Semaphore-style ZK withdrawal so a payment does not reveal *which group* someone was in, fiat-first. Semaphore and private paymasters exist; MedVault uses Semaphore for anonymous *trial applications*, not for mixed-source benefit payouts. The payment-unlinkability requirement for health benefit-sharing is a real design gap elsewhere, but the primitives are not new.
4. A portable **consent wallet** that is used both for "what my assistant may do" and for "what a researcher may do," mapped in the core (not a later export) to FHIR Consent + GA4GH DUO + ISO/IEC TS 27560, with encrypted receipts and an external transparency-log / EAS checkpoint. Each standard and several pairwise mappings exist; a single personal wallet that does all three plus assistant mandates was not found as a complete product.

**Replace or stop rebuilding:**

| Idea | Replace / reuse with |
| --- | --- |
| OpenAI-compatible routing, retries, fallbacks, per-deployment cooldown | [LiteLLM](https://github.com/BerriAI/litellm) (MIT open core). Enable native Retry-After; do not write a second router. |
| Encyclopaedic free-tier list | Official provider pages + [mnfst/awesome-free-llm-apis](https://github.com/mnfst/awesome-free-llm-apis) (CC0) as leads. The cheahjs list is deleted (404). |
| Stacking dozens of free tiers behind one `/v1` | Already the job of [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (MIT). Muse should not compete on catalogue size. |
| Subscription-to-API or account pooling | Do not build. See [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), [sub2api](https://github.com/Wei-Shaw/sub2api), and [Anthropic's Usage Policy](https://www.anthropic.com/legal/aup). |
| Lean bounty marketplace as such | [conjectures.io](https://conjectures.io) (Bittensor SN66) and [theorem-marketplace](https://github.com/wadimiusz/theorem-marketplace). Integrate as a worker / differentiator, do not clone the market. |
| Generic LLM privacy / no-train routing | [Vercel AI Gateway](https://vercel.com/docs/ai-gateway/security-and-compliance/disallow-prompt-training) and similar ZDR flags. Keep a self-hosted personal version; do not claim the category. |
| Selling chat traces at a contributor-set price | [thot.market](https://thot.market/) already does this. Muse's plan already refuses a free-text conversation market (C3). |
| Generic clinical annotation labour | Mercor, Handshake, and similar. Out of scope (design plan §10.1). |
| Confidential compute / federated training platform | Rhino FCP, Owkin, Apheris, Flower, NVIDIA FLARE, UKB-RAP. Partner, do not rebuild. |
| x402 payment plumbing | Official x402 + small adapters such as [x402-tollbooth/gateway](https://github.com/x402-tollbooth/gateway) (MIT, 3 stars). Muse's work is *what* is sold, on testnet only. |
| One on-chain contract per person for data rights | Dropped in v0.2 for a receipts layer, correctly. Phase 0 `DataVaultRegistry` must be redesigned, not shipped as-is. |

---

## Part A — Muse Works and the personal gateway

### A1 Routing, catalogue, cooldown

| Component | Class | Notes |
| --- | --- | --- |
| LiteLLM proxy, aliases, fallbacks | (a) | Reuse [LiteLLM](https://github.com/BerriAI/litellm). Optional later alternative: [Bifrost](https://github.com/maximhq/bifrost) (Apache-2.0). |
| Enabling native Retry-After (today `cooldown_time: 30` is still hardcoded in `scripts/render_config.py`) | (a) / fix | LiteLLM 1.108.5 already supports per-deployment Retry-After. This is a configuration bug, not an invention. Upstream any remaining gap. |
| Cross-alias / daily-quota blocks shared by one person's apps | (b) | Gateways cool one deployment. Muse's ledger + hook share a block across aliases and apps, and ration interactive vs background work under a daily paid cap. Keep that difference; it is small and concrete. |
| Dated, source-cited personal catalogue + read-only scanner | (b) | Lists exist ([mnfst/awesome-free-llm-apis](https://github.com/mnfst/awesome-free-llm-apis)). Muse's difference is: official pages only, `checked_on` + confidence, no auto-signup, expiry in one dashboard with *usage*. Worth keeping if the sync stays honest. |
| Privacy-class routing (`local` / `private` / `standard` / `public`) and raise-only detectors | (b) | [Vercel AI Gateway](https://vercel.com/docs/ai-gateway/security-and-compliance/disallow-prompt-training) and other ZDR routers already filter no-train / no-retention providers. Muse's difference is self-hosted, catalogue-dated policy (`unknown` is not private), and a detector that only *raises* a class. Do not advertise as unique. |
| Output licence header (`x-muse-output-licence`) | (b) | Licence metadata on model outputs appears in various gateways and dataset cards. A consistent header into the receipts layer is a small, useful convention, not a new field of research. |
| Consumer-subscription automation | **out of scope / (a) in a dangerous form** | Already a crowded and banned-or-risky category. Muse's hand-off-only design is the correct non-feature. |
| Supply-tier policy (ToS-risk ranking of compute) | (b) | Policy engines in Portkey / LiteLLM can tag deployments. Ranking *terms risk* (training, review, residency) for leftover-quota jobs is a product rule, not a new protocol. Keep as config. |

### A2 Settlement (x402, invoices, testnet)

| Component | Class | Notes |
| --- | --- | --- |
| x402 exact / upto / auth-capture on Base Sepolia | (a) for the protocol, (b) for the application | The protocol and facilitators already exist. Tiny OSS gateways exist ([x402-tollbooth/gateway](https://github.com/x402-tollbooth/gateway), [fareboxfun/platform](https://github.com/fareboxfun/platform)). Muse should use the official schemes (no facilitator callback — already corrected in v0.2) to price *verified work, data access, or judgment*, fiat invoice in parallel, testnet only until audit + KYC. |
| Quote binding, nonce, expiry (phase 0) | (b) | Normal payment-request hygiene; keep the implementation, do not claim novelty. |
| Fiat-first, plug-in rails (invoice for pharma, x402 for agents) | (b) | Ordinary commercial design. Necessary, not novel. |

### A3 Useful work and verification signals

| Component | Class | Notes |
| --- | --- | --- |
| Lean kernel + statement lock + banned tactics + sandbox | (a) / (b) | conjectures.io already uses Comparator + Lean kernel (optional nanoda) on formal-conjectures. Phase 0 already copies the right shape (`docs/LEAN.md` on PR #2). Reuse that pipeline; pin a current Lean; prefer lake comparator + a second kernel when selling a result. |
| Public Lean bounty market | (a) | [conjectures.io](https://conjectures.io) is the direct competitor (TAO, submission fee, human review). [theorem-marketplace](https://theorem-marketplace.com) / [wadimiusz/theorem-marketplace](https://github.com/wadimiusz/theorem-marketplace) (MIT, 2 stars) pays ETH for Lean proofs. Do not open a third general bounty site first. |
| Leftover personal official-API quota → verified tasks that benefit the contributor | **(c) as a combination** | People already spend *their own* ChatGPT/Claude quotas on Lean (blog reports, not products). Job queues for leftover free-tier tokens exist in Muse (`muse/queue.py`) and in generic batch tools. No shipped product was found that automatically spends idle *official* personal quota on a locked statement, sandboxes a kernel check, and returns the artifact or a dollar bounty / signal credit to that person. This is the best Part A novelty. Demand is unproven (§9.5). |
| Verification-signal leaderboard (success rate by model, task type, cost), dollar sponsors, no token, no submission fee | **(c) as a combination, with a demand risk** | Arena rankings and SN66 proof counts exist, but not this exact sold signal. Be honest in marketing: "labs will pay for this" is a hypothesis, not a fact. Kill criteria in the 90-day plan apply. |
| sorry-fill, Dafny, Verus, contract verification | (b) | Each has research tools and some contests. Expanding the *signal* beyond formal-conjectures is a good differentiator versus SN66, not a new verifier. |
| Contributor-set prices for de-identified *structured* usage / judgment (no free text) | (b) | [thot.market](https://thot.market/) lets people set a price and licence for AI traces (token-denominated). GOAT Labs pays a platform-set cashback on coding traces. Mercor et al. buy judgment at buyer-set rates. Muse should not enter the raw-chat market (already excluded). A *structured*, batch-threshold, unlinkably paid, fiat-first feed is only a difference if those constraints are real. |

---

## Shared infrastructure in the design plan

| Component | Class | Notes |
| --- | --- | --- |
| Receipts layer (signed receipts, append-only Merkle log, periodic EAS anchor, crypto-shredding) | (b) | Certificate Transparency, Trillian, EAS, and various "consent receipt" products already exist. The novelty is using one receipt schema for consent, provenance, verification, access and payment, without a per-person contract. Worth building as *infrastructure for A and B*, not as a standalone product. |
| Standards mapping: ISO/IEC TS 27560, FHIR Consent, GA4GH DUO | (b) | The standards are public. [LifeCare describes ISO 27560 receipts in production](https://lifecare.id/consent-receipt). [GA4GH MRCG](https://www.ga4gh.org/product/machine-readable-consent-guidance-mrcg/) maps consent form language to DUO. [MyRxWallet](https://myrxwallet.io/) issues FHIR Consent and anchors grants on Hyperledger. A *triple* mapping plus assistant mandates in one portable wallet is the scarce bit (see health spec). |
| Unlinkable revenue pool (global pool, Merkle amounts, Semaphore-style ZK withdraw, fiat via a licensed partner) | (b) leaning **(c) in this application** | [Semaphore](https://semaphore.pse.dev/) is the primitive. Paymasters already sponsor anonymous members. [MedVault](https://med-vault.xyz/docs/semaphore) uses Semaphore so a trial *application* is unlinkable. Using a mixed-income pool so that *paying a cancer-cohort member does not reveal that membership to the public or the buyer* was not found as a shipped health-stewardship product. Primitives are off-the-shelf; the product rule is the contribution. Residual fingerprinting of amounts is already noted in the plan. |
| No token, demand-first, mainnet only after audit + KYC | (b) | A governance choice. Good, not novel. |
| MIDATA-like cooperative steward + commercial contracting agent | (b) | [MIDATA](https://www.midata.coop/) (and similar co-ops) already exist. The split (Taiwan non-profit / co-op + Ceresus as commercial agent) is an organisational design, not software novelty. |

---

## Part B — health stewardship and the health-bot spec

| Component | Class | Notes |
| --- | --- | --- |
| Living cohorts: patient-carried cross-hospital records + clinician annotation + recontact | (b) | UK Biobank, All of Us, MIDATA, and trial-recruitment vendors all have pieces (longitudinal data, or recontact, or institution-held records). Pharma already buys recontact through sites and CROs. Muse's claimed edge is *the patient* assembling cross-hospital history and granting recontact under a steward, in a Taiwan regulatory setting. That is a positioning and legal design, not an empty field. It remains the right Part B bet versus "become Mercor for zh-TW clinicians." |
| Access tiers T1 query / T2 enclave train / T3 supervised session / T4 recontact; no raw imaging or genome transfer | (b) | Explicitly modelled on the UK Biobank RAP turn (data does not leave; outputs are reviewed). Owkin, Rhino, Lifebit, DNAnexus and others sell enclave or federated access. Do not rebuild the enclave platform. The difference is patient-side consent and pricing of *tiers*, not the TEE. |
| Patient-set prices per tier, on-chain rights registry, encrypted receipts | (b) | Data marketplaces and vault registries exist (and often failed or pay the institution, e.g. Truveta; LunaDNA closed). Phase 0's per-person `DataVaultRegistry` was the wrong on-chain shape (linkability) and v0.2 already replaces it with receipts. Contributor-set tier prices plus receipts is a product experiment, not a new cryptographic object. |
| Client-side decryption (WebCrypto), server holds ciphertext only | (b) | Standard E2E pattern (password managers, many health wallets). **Required**, and it is why the current `muse/trust` server-side unlock is not v0.2. Residual trust during a live request is honestly documented in the spec. |
| Passkey / WebAuthn for sensitive mandates | (b) | Standard. Keep. Do not sign sensitive grants with a vault key the assistant also holds. |
| Encrypted receipts, HMAC refs (not raw SHA-256 of plaintext), external checkpoints (transparency log / EAS) | (b) | Individual techniques are known. They are the right hardening of the hash chain on the health branch (HB-03, HB-04). |
| Consent wallet mapped to FHIR Consent + DUO + ISO 27560, used for both the assistant and research grants; re-consent on issuer change | **(b) leaning (c) as a composition** | Pairwise mappings exist (see above). A portable wallet that (1) is the same object for "my assistant may read this record" and "this researcher may use it at tier T2," (2) exports all three standard views, (3) must be re-signed when the controller changes from hospital to company — that composition was not found as one product. Still: the standards do the heavy lifting; Muse's job is a faithful, exportable mapping and a painful-but-correct re-consent flow. |
| Gateway attestation signed on every model response | (b) | Supply-chain / response signing appears in various proxy designs. Keep as a defense against misconfiguration, as the spec already limits its threat model. |
| Emergency / non-diagnosis rules with a zh-TW labelled set | (b) | Ordinary safety design for a health assistant. Necessary for a pilot; not a research contribution. |
| Taiwan-specific legal path (醫療法施行細則 §49-1 copies vs abstracts, 健康存摺 localisation, 人體研究法, cooperative steward) | (b) | Regulatory work, not software novelty. It can still be a practical barrier that foreign platforms do not clear. |

---

## Phase 0 code (PR #2) versus this map

| Phase 0 module | What to do |
| --- | --- |
| `muse/gateway/*` (router, circuit breaker, Retry-After) | Prefer LiteLLM native behaviour; keep only tests that the real `:4000` path honours Retry-After and cross-alias blocks. |
| `muse/settlement/*` (x402 quotes, mock pay, allow-list) | Keep as a testnet adapter; align with official x402 v2; no mainnet. |
| `muse/useful_work/*` (Lean lock, sandbox, samples) | Keep and tighten against conjectures.io's verifier (comparator + kernel + sandbox). This is the seed of scarce item 1, not a marketplace yet. |
| `contracts/src/DataVaultRegistry.sol` | Do not ship the v0.1 "one contract per person" design. Redesign around the receipts layer or park the contract. |
| Import name `muse` | Collides with the personal-gateway package. Separate environments until a rename is agreed. |

---

## Honesty limits

- "Genuinely scarce" here means "no public product found in searches on 2026-11-13 that ships this combination." Private labs, unindexed Chinese-language tools, or papers may exist.
- Several (c) rows are *compositions of known parts*. That is still a valid product strategy if the composition is what users need and if Muse does not also try to win on (a) commodities (another free-tier aggregator, another TEE, another bounty token).
- Part A demand for paid verification signals is explicitly unverified in the design plan. Novelty does not cancel the 90-day kill criteria.
