# Muse Stack × Ceresus — design plan v0.1 and PRs #1/#2: review

Reviewed 2026-11-12. Covers the 57-page design plan, PR #1 (gateway) and PR #2 (phase 0, four layers).
Not legal advice. Items marked **[verified]** were checked against primary or multiple sources today; **[judgment]** is my own analysis; **[unverified]** could not be confirmed.

---

## 1. Verdict

The plan is unusually honest (placeholders marked, COI disclosed, legal questions listed) and its core instincts are right: sell outputs not quota, no token, minimal on-chain data, non-medical first. But it has five structural problems that documentation polish will not fix:

1. **Layer 3 has no buyer and almost no yield.** Free/small models do not prove open conjectures, nobody was found buying third-party proofs, and a direct competitor already occupies the exact niche.
2. **The "leftover quota" supply is mostly off-limits.** Consumer subscriptions and several free tiers prohibit exactly this pattern, and providers enforced it with bans during 2026.
3. **The payout design puts a Singapore company in the one crypto regime where the regulator says it will generally not license.**
4. **The medical layer's supply assumptions fail in Taiwan** for the highest-value modalities (WSI, raw WGS), and its on-chain design leaks diagnosis through payouts.
5. **Four layers are four businesses.** The shared-infrastructure argument is weaker than the plan claims; the crypto rail also contaminates the trust brand the medical layer needs.

My recommendation: keep the principles, cut the scope to one wedge with a real buyer, and split the medical work into a separate fiat-first, regulator-facing track. Details in §5 and §8.

---

## 2. What is strong (keep)

- **D1/D4/D5** — outputs not quota, pay on verified results, pull-mode nodes with credentials staying on the member's machine. This is the only defensible legal shape.
- **D2/P7** — no token. Correct, and it differentiates from every decentralised-AI network.
- **D6/P6** — minimal on-chain footprint. Direction is right; the implementation needs changes (§4.4).
- **D10/P8** — staged release, medical last, with explicit "do not touch real patient data before counsel + IRB".
- **§11.3 COI controls** — recusal from hospital selection, IRB, recruitment and negotiation. Better than most plans at this stage.
- **Contributor floor (§12.3)** rather than a platform-fee cap. A genuine fiduciary signal.
- **"Why" boxes and 待查證 markers.** They made this review possible.

---

## 3. Corrections to facts in the plan

| Plan says | Actual state | Status |
|---|---|---|
| §5.2/5.5: facilitator transfers USDC then calls `settle(quoteId, …)` on `MuseSettlement` | In x402 `exact`, the facilitator only calls `transferWithAuthorization` (or the Permit2 proxy) to `payTo`. No callback, no arbitrary call, nothing binds a payment to an order id on-chain. The contract as specified cannot work with a standard facilitator. | verified |
| §5.2: x402 v1 body, v2 "to be checked" | v2 (9 Dec 2025) is current: `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE`, CAIP-2 networks. Governed by the x402 Foundation under the Linux Foundation (operational 14 Jul 2026; Stripe, Visa, Mastercard, Google, AWS, Circle, Cloudflare are members). | verified |
| §5.6: custom dispute window and refund | x402 now has `auth-capture` (escrow with authorize/capture/void/refund, client-signed fee bounds), `upto` and `batch-settlement` schemes. No protocol-level arbitration. | verified |
| §12.6: "Bittensor Subnet 66" as a generic verifiable-work comparator | SN66 is conjectures.io: paid Lean proofs of formal-conjectures targets, first valid proof wins, verified with Comparator + kernel. It is the same niche as Layer 3. | verified |
| §6.4: `grep sorry`, `#print axioms`, statement text hash, dual replay | Insufficient. `debug.skipKernelTC`, `Lean.ofReduceBool`, `@[implemented_by]`, local notation/instances and elaboration-time IO all defeat naive checks. The Lean reference manual recommends `lake comparator` (in core since v4.35) for proof marketplaces. Real kernel soundness bugs were fixed in 2026, so pin a recent Lean and use a second kernel (nanoda). | verified |
| §10.6: Taiwan "VASP registration" | The Virtual Asset Service Act was promulgated 22 Jul 2026: licensing, seven VASP categories, criminal penalties. Commencement date pending. | verified |
| §10 disclaimer: Taiwan PDPA amendment / PDPC "progress" | Amendment promulgated 11 Nov 2025 but not in force; the PDPC is still a preparatory office. MOHW remains the health regulator. | verified |
| §12.5: UK Biobank "£1,000–3,000 per year" | £3k / £6k / £9k per three years by tier, plus compute. After UKB data was listed for sale on Alibaba (Apr 2026), UKB is moving to results-only export. | verified |
| §12.5: Spirit auction | Real (Google US$10M, Mercor US$7.5M backup), but not yet approved; hearing 17 Nov 2026. It is internal enterprise data with customer data excluded, so it is not a personal- or health-data price signal. | verified |
| §12.5: Truveta US$320M, LunaDNA closure | Both correct. Note Truveta pays institutions, and LunaDNA paid members cents to ~US$21 in illiquid shares. | verified |
| §4.4 model list | Cohere Command R7B and Command A (03-2025) are under retirement notices; Gemini 3.1 Flash-Lite is being superseded. LiteLLM v1.108.5 exists (v1.108.6 followed). | verified (medium) |
| §4.2 "gap": LiteLLM cooldown is a fixed 30 s, Retry-After breaker needed (BL-03, P0) | LiteLLM 1.108.5 already takes cooldown from the response `Retry-After` header (seconds and HTTP-date). BL-03 as written is largely redundant. | verified by reading the pinned wheel |
| §1.4: layers 2–4 "not started" | PR #2 (opened today) contains skeletons of all three. | verified |

Statute citations I spot-checked (個資法 §§3, 6, 10, 21, 51; 醫療法 §§71, 72; HIPAA 164.502/514/524; PDPA §§13–26A; FSMA Part 9 date; GDPR articles) are correct. One trap: 醫療法施行細則 §49-1 concerns Chinese-language summaries, not copy fees or deadlines.

---

## 4. Critical findings by layer

### 4.1 Layer 1 — routing

- **Pooling members' free tiers for coordinator-assigned work looks like quota farming** even in pull mode. The "member sells own work" framing weakens when the coordinator supplies the task, prompt and pipeline. Cohere trial keys are expressly non-commercial; Gemini free tier trains on content, allows human review, and may not serve EEA/UK/CH users; OpenRouter `:free` commonly routes to training providers. **[verified terms; judgment on characterisation]**
- **Consumer subscriptions are effectively closed.** Anthropic's current text limits subscription OAuth to "ordinary, individual usage" and bars intermediating usage; Google suspended AI Pro/Ultra accounts for this pattern through 2026. OpenAI's "Sign in with ChatGPT" is the only sanctioned route and it is partner-gated. O4 should be answered "no" now. **[verified]**
- **`x-muse-data-class` is self-declared.** An honour-system header cannot carry CC-01 ("C4 never enters the gateway"). Default unknown traffic to C2, add a PHI detector as defence in depth, and state that detection is best-effort. **[judgment]**
- **The plan ignores the one compute source with zero ToS risk: the member's own hardware running open-weight models.** See §5.2.

### 4.2 Layer 2 — settlement

- **Custody.** A Ceresus-multisig contract that holds buyer funds through a dispute window, can refund and can pause is not "non-custodial" in substance. Splitting buyer payments to overseas third parties risks characterisation as transmitting digital payment tokens; with overseas recipients that lands in the DTSP regime, where MAS has said it "will generally not issue a licence" and gave no transition period. Accepting USDC for Ceresus's own services is likely out of scope. No MAS guidance on smart-contract arrangements was found. This is the single biggest legal risk in P1–P2 and belongs above R8's "medium". **[verified regime; judgment on scope]**
- **Per-copy sale of proofs does not hold up economically.** A proof is copyable the moment it is delivered, AI-generated output has weak or no copyright protection, and open-problem proofs are public goods. "Contributor owns the result" (§6.5) leans on rights that may not exist. The natural models are bounties (sponsor pre-funds, result is public) and licensed datasets. **[judgment]**
- **Sequencing bug.** M1.6 puts the settlement contract on mainnet in P1; the external audit (M2.4) and KYC/sanctions screening (BL-19) are in P2. DPO and governance are also P2, although accounts and wallets (C2 personal data) are collected in P1.
- **"First to register wins" needs commit-reveal.** Otherwise the verifier operator, or anyone watching submissions, can front-run.

### 4.3 Layer 3 — useful work

- **Yield.** DeepMind's own agent resolved 9 of 353 open Erdős statements at a few hundred dollars each. Nothing suggests gpt-oss-20b or Flash-Lite-class models prove open statements; textbook-level Mathlib lemmas are the realistic ceiling. **[verified; inference on small models]**
- **Demand.** No evidence that Harmonic, Axiom, Math Inc, Principia or DeepMind buy third-party proofs or attempt traces. Paying venues are SN66 (paid in subnet token) and a few prizes. **[absence of evidence]**
- **"Verified" ≠ "valuable".** formal-conjectures statements are sometimes misformalised (e.g. issue #7493: Erdős 450 and 552 false as stated), and many AI "solutions" turned out to be in prior literature. A statement-fidelity and literature check has to sit beside the kernel check.
- **Training-data resale conflict.** Anthropic, OpenAI and Google all bar using outputs to build competing models. Selling prover traces generated on their models to a lab that trains provers exposes the member's account. **[verified clauses; judgment on exposure]**

### 4.4 Layer 4 and the medical layer

- **Supply.** 醫療法 §71 gives a right to record copies; DICOM CDs are routine. But no rule requires a hospital to scan or hand over whole-slide images, and no rule covers raw WGS files. The two modalities the plan values most depend on hospital goodwill. **[verified]**
- **Hospital signature.** EMRs are already signed inside the HIS with 醫事憑證, but no patient-facing export carries a verifiable signature and no duty to sign one exists. The hospital gets nothing in the current design, so R14 ("hospital refuses") is the default outcome, not a medium risk. **[verified; judgment]**
- **Payout linkage reveals diagnosis.** `join(cohortId, vault)` plus a USDC payout that the patient cashes out at a KYC exchange tells the exchange and any chain analyst that this person is in, say, a liver-cancer cohort. Derived addresses do not help because funds consolidate at cash-out. The EDPB's final blockchain guidelines (v2.0, 7 Jul 2026) treat salted hashes as personal data for the operator and require that a data subject's transactions not be linkable. O7 is not optional. **[verified guidance; judgment on attack]**
- **Federated learning over personal vaults has no precedent.** Every production deployment found is institution-level. A 1–3 GB slide or 100 GB genome in a patient's Google Drive cannot be a training node. Tier 2 as drawn is fiction; the real architecture is "encrypted blob + key release to an attested enclave". **[verified]**
- **Enclaves.** Battering RAM, WireTap and TEE.fail (2025) forge attestation with cheap physical interposers; Intel and AMD consider this out of scope. Attestation no longer proves who physically holds the machine, so the enclave must never run on buyer-operated hardware. **[verified]**
- **Tier 4 (full copy) is the tier the market is abandoning** — see UK Biobank's move to results-only export.
- **De-identification.** Faces are reconstructable from defaced head MRI; WSI re-identification across slides reaches F1 ≈ 0.8; WGS is identifying by nature. Treat every tier as personal data.
- **"受託人 / fiduciary".** In Singapore the label invites the Trust Companies Act question and real equitable duties. Use it only after counsel confirms the structure. **[judgment; no MAS/PDPC guidance found]**
- **Policy headwind in Taiwan.** The NHI Data Management Act (in force 10 Aug 2026) bars commercial use of NHIA-held data and claws back industrial benefit. It does not govern patient-held copies, but it shows where the legislature stands. The Biobank Act (MOHW licence, export approval, ≥50% group benefit-sharing) and 人體研究法 §15 (collective consent and commercial-benefit agreement for research involving indigenous peoples) can each be triggered if a pooled cohort is characterised as a biobank or as human research. §15 is missing from the plan. **[verified]**
- **健康存摺 SDK** is the only source-authenticated patient channel in Taiwan, but it requires storage in Taiwan unless NHIA approves, purpose limitation and annual security certification. It conflicts with a Singapore-hosted resale model. **[verified, 2024 text]**
- **Precedent.** No patient-paid health-data intermediary was found paying material cash in 2026. LunaDNA, Nebula, CitizenMe and Japan's information banks died or stalled; MIDATA survives as a non-paying research co-op. Truveta and Protege pay institutions. The plan reads LunaDNA's failure as "not enough individuals"; the evidence says "not enough funding and trivial payouts".
- **Gaps:** minors, incapacity and deceased persons; return of clinically actionable incidental findings; undue-inducement review of per-subject payment; genetic discrimination (Taiwan has no dedicated statute); tax reporting (CARF/DAC8); the US DOJ bulk-data rule; Washington MHMDA sale authorisation.
- **De-identified chat-log market (§7.2).** PII masking does not anonymise free text; logs contain third parties' data, employer confidential material and proprietary code. `k` batching does nothing for text. Highest risk-to-revenue ratio in P2. **[judgment]**

### 4.5 Strategy

- A developer gateway, an agent-payments rail, a proof bounty network and a regulated health-data trust have different customers, regulators and trust requirements. Only consent records, receipts and audit logs are really shared.
- All KPIs and prices are TBD, there are no kill criteria, and no buyer has been interviewed. Demand is the plan's largest unknown and its least-examined one.
- P1 (gateway, breaker, policy engine, node CLI, Lean verifier, mainnet contracts, two-jurisdiction legal opinion) is not a three-month scope.

---

## 5. Proposals

### 5.1 Pick one wedge: verified-clinician judgment in Traditional Chinese
This is where the author's position is an asset rather than a conflict.
- Buyers exist and pay: physician rates on Mercor / Handshake / micro1 run roughly US$70–300/hour. **[medium-confidence sources]**
- Differentiator: **licence verification by 醫事人員憑證 signature** (and MOHW's digital 醫事資格證明, physicians planned early 2027), where incumbents vet by AI interview.
- No patient data, no crypto requirement (pay by bank transfer; USDC optional), no hospital dependency.
- It builds the clinician network, consent tooling and buyer relationships the medical layer needs later.
- Caveat: I found no evidence of paid commercial demand specifically for zh-TW medical evaluation. Get three buyer LOIs before building.

### 5.2 Redefine "owned compute"
Rank supply by ToS risk and build in that order:
1. Member's own GPU/Mac running open weights (no provider terms).
2. Member's paid API keys.
3. Provider-sponsored quota under a written programme (make §4.12's last bullet a prerequisite, not an option).
4. Free tiers — Ceresus's own gateway use only, not member pooling.
5. Consumer subscriptions — excluded.

Add an **output-licence class** to routing (`x-muse-output-license`): work destined for sale as training data routes only through open-weight models whose terms allow it.

### 5.3 Layer 3: from "mining" to sponsored, verifiable bounties
- Sponsor pre-funds; result is published openly; contributors are paid on verification. No per-copy sale.
- Verifier = `lake comparator` + nanoda in a real sandbox (gVisor/Firecracker, no network, no environment), plus a statement-fidelity and prior-literature step.
- Broaden beyond open conjectures to work with payers: `sorry`-filling in active formalisation projects, verified-software tasks (Dafny/Verus), smart-contract formal verification (audit contests already pay), hidden-test code tasks.
- Position against SN66 on "no token, paid in dollars, results open".

### 5.4 Settlement without custody
- **Immediate, immutable split:** `payTo` = an ownerless 0xSplits v2 contract on Base. Ceresus only ever receives its own fee.
- **Refunds from Ceresus's own guarantee pool**, funded by the platform fee, instead of locking buyer funds. Ceresus refunding with its own money is not holding others' money.
- Where escrow is truly needed, use x402 `auth-capture` with the facilitator as operator, not a Ceresus contract.
- **Bind payment to order** by setting the EIP-3009 nonce to `hash(quoteId)`. (My inference from the spec, not a documented pattern — test it.)
- **Replace `ResultRegistry` / `ConsentRegistry` with EAS schemas** rather than custom contracts; less to audit. Note EAS on Base is upgradable by Base governance.
- **Make the rail pluggable.** x402 for agent-scale purchases; invoice and bank transfer for enterprise and all medical contracts; fiat payouts through a licensed partner. Pharma does not pay by HTTP 402.
- Alternatively restructure Ceresus as merchant of record that pays contributors as its own suppliers. Both shapes need Singapore counsel before any mainnet transaction.

### 5.5 Medical layer: sell what only a patient can assemble
A single hospital's copy is low-value and the hospital can license it better. The patient's unique assets are:
- **Cross-institution longitudinal linkage** (hospital A + B + 健康存摺 + wearables + patient-reported outcomes).
- **Recontactability** — follow-up questions, new samples, trial matching. Buyers pay most for this, and it is consent-native. Make "recontact right" a product tier.
- **Regulatory-grade provenance and consent**, which buyers need for EU AI Act Art. 10 and FDA data-lineage expectations.

Design changes:
- **Tiers:** 1 query → 2 train-in-enclave, export model only → 3 supervised enclave session with output checking (Five Safes). Drop full transfer for imaging and genomics.
- **Enclaves** in hyperscaler facilities under audited physical security; threshold key release; never buyer hardware.
- **Consent off-chain** as signed records (ISO/IEC TS 27560, HL7 FHIR Consent, GA4GH DUO codes for machine-readable use terms), appended to a transparency log with periodic anchoring. No per-patient contracts.
- **Payouts** through one global distributor that mixes all income types and carries no cohort tag (Merkle claims), or fiat. Never cohort-labelled on-chain payments.
- **Provenance without the hospital:** 健康存摺 SDK first; investigate zkTLS proofs of portal sessions as research only (no production health deployment found, and portal terms may forbid it).
- **Give the hospital a role:** cost-based attestation fee, or curation/annotation partner, or enclave site. A party that carries risk and receives nothing will not sign.
- **Framing:** benefit-sharing with a community component, not "sell your data". It fits Taiwan's Biobank Act logic and lowers R13.
- **Entity:** consider a Taiwan-resident non-profit or co-op as data steward (MIDATA model), with Ceresus as commercial contracting agent. This answers the 健康存摺 localisation rule and cross-border questions.

### 5.6 Smaller ideas
- Expert reputation as verifiable credentials (OID4VC / SD-JWT, compatible with TW DIW when physician credentials arrive).
- Publish verified results as MCP tools with x402 pricing and Bazaar metadata; that is where agent buyers discover things.
- Sybil resistance by layered proof: Human Passport attestations on Base, or World ID; professional tiers by 醫事憑證.
- A per-model "verified success rate" leaderboard from real work — a by-product buyers and providers both want.

---

## 6. Ecosystem map

| Area | Who/what | Relation |
|---|---|---|
| Agent payments | x402 Foundation; Coinbase CDP facilitator (free to 1,000 settlements/month, then US$0.001; OFAC/KYT on both sides); Cloudflare Monetization Gateway; x402 Bazaar | Build on |
| | Stripe MPP / Tempo; Circle Gateway nanopayments; Mastercard Agent Pay | Competing or adjacent rails — keep the rail pluggable |
| | Google AP2; Visa Trusted Agent Protocol; ERC-8004; Skyfire | Authorisation / identity layers — complementary |
| Base primitives | EAS, 0xSplits v2, Safe; Kleros via bridge (no native Base court) | Replace custom contracts |
| Proof verification | `lake comparator`, nanoda, SafeVerify, Lean4Lean, AXLE | Use, don't rebuild |
| Proof demand | conjectures.io (SN66); legacy Erdős prizes; Epoch FrontierMath; Palomar registry; Mathlib Initiative (no bounties) | Competitor / thin demand |
| AI-math labs | DeepMind, Harmonic, Axiom, Math Inc, Principia | Generate own data; not buyers so far |
| Decentralised AI work | Gensyn, Ritual, Vana, Sahara, Prime Intellect | Token-based comparators |
| Expert labour | Mercor, Handshake AI, Surge, Turing, micro1, Outlier, Centaur Labs | Competitors and channel partners for Layer 4 |
| Data licensing | Protege, Segmed, Gradient Health, Datavant, Tempus, Flatiron, Truveta | Institution-side incumbents; possible distribution partners |
| Patient-side precedents | MIDATA; LunaDNA, Nebula, CitizenMe, Hu-manity (dead/stalled); Vana DNA DAO; 23andMe → TTAM (~US$20/customer) | Lessons |
| Privacy compute | Rhino FCP, Owkin, Apheris, BeeKeeperAI, Lifebit, DNAnexus/UKB-RAP, Flower, NVIDIA FLARE; OHDSI/OMOP | Partner rather than build |
| Consent / access standards | GA4GH DUO + Passports; HL7 FHIR Consent; ISO/IEC TS 27560; Five Safes; SMART Health Links; W3C VC 2.0, SD-JWT | Adopt |
| Taiwan | 健康存摺 SDK; MyData 平臺; TW DIW; TW Core IG / FHIR Box; 醫事憑證; Taiwan Biobank; NSTC imaging database (dynamic consent); draft 促進資料創新利用發展條例 | Build on / watch |
| Asia | Korea Health Information Highway + specialised MyData institutions; Singapore TRUST, HIA (uncommenced; no portability right); Japan 次世代医療基盤法 | Models and constraints |
| EU / US | EHDS (secondary use 2029/2031 via access bodies; individual sale not contemplated); DGA intermediaries (EU representative required); TEFCA IAS; CMS Health Tech Ecosystem; MHMDA; DOJ bulk-data rule | Market-entry constraints |
| Identity / Sybil | World ID, Human Passport, zkPassport/Self, Coinbase Verifications | Options |

---

## 7. PR review

CI is green on both. PR #2 is stacked on PR #1's branch, not `main`. Locally: 44/44 pytest pass and `validate_config.py` passes on both trees. Foundry tests were not run locally (CI reports them green). Real-Lean findings were confirmed with a stub `lean` binary that records its input; behaviour under real Lean is inferred.

### PR #1 — gateway: good, mergeable after small fixes
- Rendered config holds only `os.environ/…` references; paid providers are correctly gated.
- The mutual fallback cycle (`fast↔cheap↔smart`) is not an infinite loop in the pinned LiteLLM.
- **Likely bug:** the same upstream model is registered under separate deployment ids per alias, so a 429 cooldown on `fast` does not cool `cheap`, and the fallback re-hits the same exhausted quota.
- Several model ids are absent from LiteLLM's price map (all OpenRouter `:free`, NVIDIA NIM, one Novita, Jina embeddings) — they route but have no cost/context metadata.
- CI runs every job twice; image pinned by tag, not digest; no test asserts fallback behaviour.

### PR #2 — phase 0: honest about mocks, but three claims do not hold. Do not merge as is.

| # | Finding | Where | Confirmed |
|---|---|---|---|
| 1 | Real-mode Lean lets the proof replace the theorem: output starting with `theorem …` discards the problem statement, so `theorem x : True := trivial` is marked verified and listed for sale. Only checks are exit code and a `sorry`/`admit` substring. | `muse/useful_work/lean.py:233-243` | With stub |
| 2 | Real-mode Lean runs model output unsandboxed with the full environment, including every provider key; `#eval IO.Process.run` would execute at elaboration. | `lean.py:152-163` | With stub |
| 3 | The circuit breaker gates nothing. `allow()` is only reached from code with no caller outside tests; it is not connected to LiteLLM. After a 429 with `Retry-After: 3600` the next run still hit all three aliases. | `runner.py:132-152`, `gateway/router.py:33` | Yes |
| 4 | The premise is wrong: LiteLLM 1.108.5 already honours `Retry-After`. | `docs/ARCHITECTURE.md:120-122` | Read in wheel |
| 5 | Paywall bypass: `GET /v1/work/jobs/{id}` returns the proof that `GET /v1/results/{id}` charges for. | `server.py:238-251` | Yes |
| 6 | With `LITELLM_MASTER_KEY` unset (default) every endpoint is unauthenticated, and the phase-0 compose file publishes on `${MUSE_BIND}`. Anonymous job creation can spend quota. | `server.py:148-154` | Yes |
| 7 | x402 verifier: no nonce store, no validity window, no quote expiry; the same header was accepted repeatedly and on a different result. Unsigned top-level `amount`/`payTo` override the signed authorization — harmless in mock mode, a real hole once a real verifier is built on these helpers. | `settlement/verifier.py:79-133` | Yes |
| 8 | Breaker defects: a late success closes an open circuit; a probe that never reports blocks the provider indefinitely; any 4xx counts as provider failure; wall-clock time, no jitter, process-local state. | `gateway/circuit_breaker.py` | Yes |
| 9 | Malformed payment headers and `Retry-After: "²"` return 500 with exception text. | `settlement/x402.py:39-53`, `retry_after.py:39-41` | Yes |
| 10 | Sale is recorded before delivery can fail. | `server.py:297-302` | Yes |
| 11 | Mock Lean accepts any text containing an expected tactic ("this is not a proof, it just mentions rfl") and sells it as `verified`. | `lean.py:227-230` | Yes |
| 12 | Problem loader cannot parse real formal-conjectures files (drops imports, matches "theorem" in comments, id collisions). | `problems.py` | Yes |
| 13 | App construction runs a job, blocking startup and spending quota on each restart. | `server.py:103-124` | Yes |
| 14 | `DataVaultRegistry.sol`: no hospital allow-list, so a patient can self-sign provenance; `vaultId` can be squatted; no attestation expiry or revocation. EIP-712 logic looks correct. | `contracts/src/` | Read only |

What is good in PR #2: receipts say `settled: false` / "no funds moved"; settings refuse any non-mock facilitator; missing Lean yields an error, not a silent pass; keys are never logged or returned; the Solidity has no token paths or deploy script.

Recommended changes: delete or wire the breaker (prefer a thin LiteLLM callback only for what LiteLLM lacks: quota-exhausted state and cross-alias cooldown); replace the Lean path with `lake comparator` in a sandbox; fix 5 and 6 before any demo; refuse to start without a master key; move `DataVaultRegistry` out of scope until §5.5's on-chain questions are settled. The PR title says "data vault"; what exists is an on-chain registry.

---

## 8. Suggested next 90 days

1. **Demand first.** Ten buyer conversations; three written LOIs for one product (my pick: §5.1). Define a kill criterion.
2. **Two legal memos before any mainnet transaction:** Singapore (DTSP/PSA scope of the payout flow; "fiduciary" wording) and Taiwan (Q-L1–L5 plus Biobank Act trigger, 人體研究法 §15, 健康存摺 SDK localisation, Virtual Asset Service Act).
3. **Merge PR #1** after the cross-alias cooldown fix. Rework PR #2 per §7.
4. **Provider conversation** about a sponsored-quota programme; publish the ToS matrix (BL-07) before any member node ships.
5. **Split the plan** into two documents: "Muse Works" (L1–L3, developer audience) and a health-data stewardship track (fiat-first, IRB-led, separate brand).
6. **Update the plan** with §3's corrections; move R8 and R14 to high; add risks for provider enforcement, payout linkage, TEE physical attacks, and no-buyer for Layer 3.

---

## 9. Key sources

- x402 spec v2 and schemes: https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md · https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md
- MAS on DTSP licensing: https://www.mas.gov.sg/news/media-releases/2025/mas-clarifies-regulatory-regime-for-digital-token-service-providers
- Taiwan Virtual Asset Service Act: https://www.loc.gov/item/global-legal-monitor/2026-09-10/taiwan-virtual-asset-service-act-enacted
- Anthropic subscription/OAuth rules: https://code.claude.com/docs/en/legal-and-compliance · Gemini API terms: https://aistudio.google.com/docs/terms · Cohere trial keys: https://cohere.com/pricing
- Lean proof validation: https://lean-lang.org/doc/reference/latest/ValidatingProofs/ · https://github.com/leanprover/comparator
- formal-conjectures misformalisation: https://github.com/google-deepmind/formal-conjectures/issues/7493 · DeepMind agent results: https://arxiv.org/pdf/2605.22763v1
- SN66: https://github.com/conjectures-io/conjectures-validator
- EDPB blockchain guidelines v2.0: https://www.edpb.europa.eu/system/files/2026-07/edpb_guidelines_202502_blockchain_v2_en.pdf
- TEE attacks: https://tee.fail/ · https://batteringram.eu/
- 全民健康保險資料管理條例: https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0060042 · 健康存摺 SDK 要點: https://www.rootlaw.com.tw/LawArticle.aspx?LawID=A040170081048100-1130312
- 醫療法 §71: https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=71 · 原住民族研究同意辦法: https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=D0130033
- UK Biobank fees and Alibaba incident: https://www.ukbiobank.ac.uk/use-our-data/fees/ · https://www.theregister.com/2026/04/23/500k_biobank_volunteers_data_listed/
- LunaDNA: https://endpts.com/lunadna-a-connector-of-patients-and-pharmaceuticals-to-shutter/ · Truveta: https://www.truveta.com/blog/news/truveta-genome-project/
- WSI re-identification: https://pubmed.ncbi.nlm.nih.gov/39316996/ · MRI face reconstruction: https://arxiv.org/pdf/2501.18834
