# Ecosystem review and integration plan

Written 2026-11-13. This reviews what is open and reusable today for each part of Muse, and says how to join the parts. It updates `docs/muse-plan.md`; where the two disagree, this document is the newer view and the owner decides.

How to read the evidence marks: **[V]** a primary page was opened and read in this review; **[S]** secondary sources or search results only; **[U]** unverified. Anything under "Recommendation" or "Idea" is judgement. Nothing here is legal advice; points marked ⚖ need a lawyer.

---

## 1. The short answer

Almost every layer the plan needs already exists as an open module. What does not exist is the thing that joins them, and that is where Muse should spend its effort.

1. **Reuse the pipes.** LiteLLM for routing, x402 for payments, Spend Permissions for bot spending limits, EAS for anchoring, Biscuit for delegation tokens, iroh for device-to-device links, Loro or Automerge for sync, SMART Health Links and FHIR for health data. None of these needs to be rebuilt.
2. **Build one thing well: the mandate.** A mandate is a plain sentence the person approves ("my helper may spend up to 2 USDC a day on these three services until 30 November"). Today that sentence has to be re-expressed by hand in five systems. Muse should compile it once into all of them and bind the results together by hash. Section 4 describes this. No product found does it.
3. **Keep building what is still ours.** Rationing by priority with queueing and pacing (nothing found does this for one person's apps), the dated catalogue (no maintained machine-readable catalogue across AI, storage, compute and chain was found), and receipts.
4. **Shrink the Rust rewrite.** The router does not need porting. The Rust core should hold only what must run inside the apps: vault, mandates, receipts, sync and the device link.

---

## 2. Four findings that need action now

| Finding | Evidence | What to do |
|---|---|---|
| **Oracle's Always Free ARM allowance is now 2 OCPU and 12 GB**, not 4 and 24. | Oracle's page: "1,500 OCPU hours and 9,000 GB hours per month ... equivalent to 2 OCPUs and 12 GB of memory" [V]. Reports date the change to mid-2026 and say free-only accounts have larger instances shut down [S]. | The VM `muse-a1` is a 4 OCPU / 24 GB shape. Check in the Oracle console whether it is being billed or is at risk of shutdown, and resize to 2 / 12 if the account is free-only. Design everything for 2 / 12. |
| **A LiteLLM release was compromised on 21 October 2026.** | LiteLLM's own incident page [V]: `litellm==1.107.0` on PyPI and the `v1.107.0`, `latest`, `main-latest` and `main-stable` image tags carried credential-stealing malware for about two hours. The bad images still carry a valid signature from the old key. An earlier incident in March is listed on the blog index [S]. | Muse pins `1.108.5`, which is not the affected version; the incident page does not mention it either way. Pin the image by digest as well as by tag, install from a hash-locked requirements file, and never use a floating tag. Keep provider keys out of any process that does not need them. |
| **GitHub Models was retired on 30 July 2026.** | GitHub docs [V]. | Not in the catalogue, so nothing to remove. It shows how fast free tiers vanish; `muse catalog check` should run on a schedule. |
| **A Singapore company serving only Taiwan users with tokens is the profile Singapore's 2025 licensing rule targets.** ⚖ | Since 30 June 2025 a Singapore firm providing digital-token services only to overseas customers needs a licence that the regulator says it will generally not grant [S]. Taiwan's Virtual Asset Service Act passed in June 2026 and takes effect no earlier than Q1 2027 [V]. | Stay on the test network, never hold users' keys or funds, keep credits non-redeemable for money, and get a Singapore and a Taiwan opinion before any mainnet step. |

One correction to earlier work in this repository: an earlier review cited "Medical Care Act Enforcement Rules §49-1" on record copies. This review could not find that article [U]. The right to a copy of one's record is in the Medical Care Act §71 itself [V]. Treat the §49-1 citation as unconfirmed until a lawyer checks it.

---

## 3. What to reuse, layer by layer

"Use" means take it as a dependency. "Learn" means copy the idea, not the code. "Avoid" has a reason.

### 3.1 Routing and free allowances

| Module | Facts | Verdict |
|---|---|---|
| LiteLLM | MIT core; two supply-chain incidents in 2026 [V/S] | **Use, hardened** (section 2) |
| Bifrost | Apache-2.0, Go, virtual keys and budgets [V] | **Keep as the exit** if LiteLLM's releases stay a worry |
| FreeLLMAPI | MIT, Node, one endpoint over about 29 providers, personal use only [S] | **Learn**: its provider list is a lead list, nothing more |
| OpenRouter | OAuth with PKCE gives the user their own key; free models 50 requests a day, 1,000 once 10 credits are bought [V] | **Use**: the best no-pasting sign-up |
| Hugging Face | OAuth and device-code flow with an inference scope [V] | **Use**: a second no-pasting provider; not yet in the catalogue |
| Vercel AI Gateway | Bring-your-own-key needs bought credits, and failed calls fall back to Vercel's billed credentials [V] | **Avoid** |
| Per-app rationing with queueing | Nothing found | **Build** (already built) |
| A machine-readable free-tier catalogue across AI, storage, compute and chain | Nothing maintained was found | **Build** (already begun); consider publishing it as dated JSON |

### 3.2 Helper, skills and tools

| Module | Facts | Verdict |
|---|---|---|
| MCP | Current spec dated 2026-07-28; now stateless; a registry is in preview [V] | **Use** |
| FastMCP | 4.1.8, Apache-2.0 [V] | **Use** to write tools |
| Agent Skills (`SKILL.md`) | Open folder format, widely supported [S] | **Use the format** |
| OpenClaw | MIT; its skill marketplace had hundreds of malicious uploads in early 2026 [S] | **Learn** from its chat-app pairing; **avoid** its marketplace and its run-anything default |
| Hermes Agent | MIT; writes and refines its own skills; no LINE support [V] | **Learn** |
| n8n, Windmill, Open WebUI | Licences that forbid or restrict embedding in a product [V] | **Avoid** embedding |
| Durable jobs | DBOS (MIT) runs on SQLite; Temporal is heavy [V] | **Use a plain SQLite job table** (already built as the queue) |

Rule that follows from the marketplace incidents: every third-party skill or tool is untrusted code. Run each in its own sandbox, pin it by content hash, show what it asks for in plain words, and never hand it a key. Keys stay behind the router.

### 3.3 The always-on VM

Design for 2 OCPU and 12 GB. At that size chat on a local model is too slow to be useful; expect a few tokens a second for small models (estimate, not measured).

What the VM is good for:

- The router and the queue, so chores run while the person's devices are off.
- The always-online sync peer, so phone and desktop meet through it.
- The LINE webhook. In Taiwan a LINE account's replies are free and only pushed messages count against the 200-a-month free plan [V]. A helper that answers inside the person's own chat costs nothing.
- Small background work: embeddings, reranking, speech-to-text with small models, OCR.
- The scheduled catalogue check.

What makes it usable by someone non-technical:

| Need | Reuse | Note |
|---|---|---|
| Create the VM without a terminal | Oracle's "Deploy to Oracle Cloud" button, which opens a stack from a zip of Terraform and cloud-init [V] | One button, sized 2 / 12 |
| Reach it without opening ports | **iroh** 1.x: devices dial each other by public key, with relays and hole-punching, and bindings for Swift and Kotlin [V] | No account, no VPN product, no open port. Cloudflare Tunnel only for the LINE webhook, which needs public HTTPS |
| Pair a phone | A QR code carrying the VM's iroh address and a one-time token | Recommendation |
| Stay up to date | OS unattended upgrades; containers pinned by digest with Podman auto-update [U] | Never a floating tag |
| Survive loss | restic or Kopia to the 20 GB of free object storage [U]; the VM must be rebuildable in one tap | Oracle can reclaim or close free accounts |
| Not be reclaimed as idle | Oracle reclaims an instance whose CPU, network and memory all stay under 20% for 7 days [V] | Keep a small model in memory |

The VM must stay optional. Everything has to work with only the desktop app.

### 3.4 App structure

| Need | Reuse | Verdict |
|---|---|---|
| Desktop and phone shell | Tauri 2.12 [V] | **Use** |
| Local database | SQLite with SQLCipher | **Use** |
| Sync between a person's devices | Loro 1.x or Automerge 0.12 [V], carried over iroh | **Use**; do not write a sync protocol |
| Encrypted group access control | Keyhive is pre-alpha and says not to use it in production [V] | **Watch** |
| On-device AI on phones | Apple's and Google's built-in models [S/U] | **Later** |

iOS allows background work only when the system chooses [S]. So a phone cannot be the always-on helper. This matters for an earlier decision; see section 9.

### 3.5 Vault, keys and mandates

| Need | Reuse | Verdict |
|---|---|---|
| File and backup encryption | age format via rage 0.12 [V] | **Use** |
| Device key storage | OS keystores through Tauri plugins | **Use** |
| Unlock with a passkey | The WebAuthn PRF extension works on recent Android, Apple and Windows, unevenly with third-party managers [S] | **Use as an option**, never the only way in; keep a recovery key |
| Delegation tokens for mandates | **Biscuit** 6.0 [V]: a signed token that anyone holding it can narrow further offline, with checks on time, amount and service | **Use** |
| UCAN | Still a release candidate [S] | **Watch**; keep the mandate shape close enough to move later |
| Bitwarden SDK | Proprietary licence limited to Bitwarden-compatible apps [V] | **Avoid** |

`muse/trust` (parked) already has a vault, mandates and receipts in Python. Its mandate format should be replaced by Biscuit rather than ported to Rust as it is.

### 3.6 Payments and chain (test network only)

| Need | Reuse | Facts | Verdict |
|---|---|---|---|
| Pay for a service call | **x402** v2 | Now under the Linux Foundation; Apache-2.0; schemes `exact`, `upto` and batch settlement; SDKs for TypeScript, Python and Go; a public test-network facilitator [V/S] | **Use** |
| Limit what the helper may spend | **Spend Permissions** | A deployed, audited contract on Base and Base Sepolia: a recurring allowance per period to one spender, revoked in one call. It has no recipient allow-list [V] | **Use first** |
| The same, with the recipient list enforced on chain | MetaMask Delegation Framework 1.3.0 (ERC-7710 / 7715) | Deployed on Base and Base Sepolia; period and target enforcers exist; pinning recipients needs composing enforcers [V, last point unverified] | **Second step** |
| A wallet a non-technical person can use | Base Account (passkey) or Coinbase embedded wallets | Free tier of 5,000 operations a month for the embedded wallet [S] | **Use for v1**; Openfort OpenSigner (MIT, self-hostable) as the exit [S] |
| Gas | Coinbase paymaster sponsors Base Sepolia; Circle's paymaster takes gas in USDC [S/V] | | **Use** |
| Anchor receipts | **EAS**, already deployed on Base and Base Sepolia; off-chain attestations are free [V] | | **Use** |
| Anchor without a chain | Transparency logs (Tessera 1.0) [S] | | **Good alternative** for people who never touch a chain |
| Helper identity and reputation | ERC-8004 registries on Base Sepolia; still a draft, no audit [V] | | **Test network only** |
| Split income | 0xSplits v2, Sablier Flow v3 [V/S] | | **Use** |
| Pay cohort members without revealing the cohort | Nothing off the shelf. Semaphore is the likely building block [V] | | **Later, with an audit** (section 6) |
| Credits | Circles v2 for decaying credits; Grassroots Economics for commitment pools [S] | | **Learn**; keep v1 credits as signed ledger entries |
| Google AP2 | Mandates in two stages, open then closed; moving to the FIDO Alliance [V] | | **Borrow the data model** |

Things to test before relying on them: whether USDC on Base accepts smart-wallet signatures for the transfer authorisation x402 uses [U], and whether any in-app route from Taiwan dollars to USDC exists [U]. Circle's nanopayments need a plain key and do not accept smart-wallet signatures [V], which clashes with a smart-account design.

The rule that keeps Muse out of custody ⚖: it never holds a user's keys, never pools users' funds in a contract it controls, never exchanges money itself, and credits cannot be redeemed for money in v1.

One concentration risk: Coinbase would supply the wallet, the facilitator, the paymaster and the chain. Keep an exit for each: OpenSigner, a self-run facilitator, and receipts that verify without Base.

### 3.7 Health data, as an add-on

| Need | Reuse | Facts | Verdict |
|---|---|---|---|
| The hospital-side door | SMART App Launch 2.2 [S] | Taiwan's health ministry has chosen FHIR and SMART for its next-generation platform; its work so far faces clinicians, and no patient-facing plan was found [V/S] | **Use**, through a hospital partner |
| Taiwan's data profile | TW Core 1.0.0 on FHIR R4 [V] | | **Use** |
| Send records from vault to clinician | **SMART Health Links** 1.0.0: an encrypted link with a passcode and expiry [V] | Library: Kill the Clipboard (TypeScript) [S] | **Use** |
| The summary payload | International Patient Summary 2.0 [S] | | **Use** |
| Oncology schema | mCODE 4.0 [S] | Taiwan's insurer has its own FHIR profiles for cancer-drug review [S] | **Use mCODE in the vault** |
| Consent model | FHIR Consent in its R5 shape; GA4GH DUO for purposes; ISO/IEC TS 27560 for the receipt | | **Use** |
| Audit trail | FHIR Provenance and AuditEvent | | **Use**, mapped to Muse receipts |
| Test servers | HAPI FHIR; Burni (Apache-2.0, hosted on the ministry's standards site) [V] | | **Use** |
| Research access without moving data | The OpenSAFELY pattern: all code public first, every job logged publicly, outputs released only after review [S] | Its job server is GPL-3.0 [V] | **Use the pattern** |
| Confidential processing | Google Confidential Space with GPUs is generally available [S]; others not re-checked | | **Later** |
| De-identification of mixed Chinese and English records | No packaged tool found | | **A gap** |

Ways for a person in Taiwan to get their own records into the vault, most realistic first:

1. **A copy of the record, photographed or as a PDF, read on the device.** The right to a copy is the patient's own (Medical Care Act §71 [V]). It depends on nobody. Extracted values stay "draft" until the person confirms them, with the source image kept.
2. **The My Health Bank SDK.** The richest structured data, with consent built in. Applicants must be Taiwan entities or Taiwan branches, pass a mobile security test, sign a contract and accept audits [S]. ⚖ A Singapore company needs a Taiwan branch or a hospital partner.
3. **A hospital's SMART on FHIR endpoint.** Technically ready, but patient-scoped access needs a willing hospital. A 2027 pilot with one medical centre is realistic.
4. **A patient summary sent by Health Link from a hospital.** Depends on hospital adoption.
5. **Taiwan's Digital Credential Wallet.** In trial since December 2025 [S]. Useful to prove identity, not to carry records. Its protocol support is unconfirmed [U].

Two legal facts that shape the business ⚖:

- The National Health Insurance Data Management Act is reported in force since August 2026 with a ban on commercial use and fines up to NT$10 million [S]. Data sold or licensed through a steward must be data the person brought themselves, never insurance-database data.
- Evidence on models explaining reports to patients: in one study a model made discharge summaries far more readable, yet 18 of 100 reviews raised safety concerns, mostly omissions [S]. No Traditional Chinese study was found [U]. The "explain my report" feature needs a regulator's view on device classification before any pilot.

Lessons from others: 23andMe was sold out of bankruptcy and about 1.9 million users deleted their accounts; LunaDNA closed for lack of funding; MIDATA, a not-for-profit co-operative, is still running [S]. The vault, keys and data must outlive the company, and paying people in a token has not worked.

---

## 4. The integration idea: one mandate, compiled everywhere

Today the same permission lives in five places that do not know about each other:

| Where | What it looks like there |
|---|---|
| The helper | "This skill may read my calendar" |
| The router | "This app may use paid models up to 1 USD a day" |
| The wallet | "This address may spend 2 USDC a day" |
| A health record | "This clinician may read my medication list until Friday" |
| The audit trail | "Consent given on this date for this purpose" |

**Idea.** A mandate is written once, in one sentence the person can read, and approved once with a passkey. Muse then compiles it to each system that has to enforce it:

| Target | Compiled form | Enforced by |
|---|---|---|
| Helper and skills | A Biscuit token, narrowed again for each skill and each job | The skill sandbox |
| Router | An app entry with priority, daily caps and a paid cap | `muse/ration.py` (exists) |
| Payments | A Spend Permission, and the same limits as client-side x402 budget rules | The contract and the x402 client |
| Health data | A FHIR Consent with DUO purpose codes | The vault |
| Proof | A consent receipt (ISO/IEC TS 27560 shape) whose hash is anchored through EAS or a transparency log | Anyone, later |

Each compiled form carries the hash of the mandate it came from. Revoking the mandate revokes all of them: the token goes on the revocation list, the Spend Permission is revoked on chain, the Consent is marked inactive, and a revocation receipt is written.

Why this is worth building:

- For the person, there is one screen that answers "what can my helper do, and what did it do?".
- For a hospital or a buyer, the permission arrives in the standard they already check (FHIR Consent, a verifiable credential, an on-chain allowance) without Muse inventing a format.
- It is the part competitors do not have. Google's AP2 has mandates for card payments; Coinbase has spending limits; FHIR has consent. Nothing found binds them.

Three more ideas that build on it:

1. **Receipts as the one audit trail.** Every action already writes a signed, hash-chained receipt. Map the same record to a FHIR AuditEvent when it touches health data, to an x402 payment reference when it spends, and publish only the chain's periodic root. A helper's public reputation (ERC-8004) can then cite proofs from that root without exposing what was done.
2. **Health Links as the zero-trust pipe in both directions.** A hospital sends a discharge summary as a link encrypted to a key the vault provides; the person forwards a narrower link to the next clinician. Muse only ever relays ciphertext.
3. **A public job log tied to personal receipts.** When research code runs on contributed data, the job is listed publicly, as OpenSAFELY does, and each contributor's vault gets a receipt naming that job's code hash. A person can show exactly which approved code touched their data, and withdraw from future jobs.

Two smaller ones from the infrastructure review that fit the product now:

- **Approvals inside LINE.** The helper replies in the person's own chat with "Pay NT$120 to ...?". A tap mints a single-use token narrowed from the standing mandate. Replies are free.
- **Spend leftovers before they reset.** The router knows each reset time, so shortly before it, queued low-priority work runs. Pacing already does most of this.

---

## 5. Zero trust, stated plainly

The promise to make, and the modules that back each part:

| Promise | How it is kept |
|---|---|
| The company cannot read your data | Decryption only on your devices; the VM is yours; relays carry ciphertext |
| The helper cannot exceed what you approved | Biscuit tokens checked at each tool call; spending limits on chain |
| A tool cannot steal a key | Tools never see keys; the router adds them |
| You can see everything that was done | Signed, chained receipts, with roots anchored outside your devices |
| You can leave | Export in open formats (FHIR, age-encrypted archive); the VM rebuilds from backup |

What it costs: if the person loses every device and the recovery key, nobody can restore the vault. The recovery design (a printed recovery key, and passkey sync as a second way in) has to be part of the first-run flow, in plain words.

---

## 6. What stays hard

- **Paying cohort members without revealing the cohort.** No ready contract. The nearest design puts every member of every cohort in one group, pays fixed amounts in batches, and uses a proof plus a one-time marker to withdraw. It needs custom, audited code, and regulators treat such designs like mixers ⚖. Amounts and timing can still give people away. Keep it out of v1; pay through ordinary channels first.
- **Traditional Chinese on small free models.** A benchmark found Traditional Chinese models trailing Simplified ones at equal size [S]. Build a small local test set before ranking free models for Taiwan users.
- **Free tiers move.** Oracle halved its allowance, GitHub Models closed, LINE raised prices, all within months.
- **Supply chain.** LiteLLM twice, a skill marketplace once. Pin by hash, sandbox, and assume the next one.

---

## 7. What an always-on VM changes for a non-technical person

Without it, Muse is an app that works while the computer is on. With it, the helper keeps going: chores run overnight on leftover allowance, the phone gets an answer in LINE, and devices stay in step. The person's part should be three taps:

1. Tap "Add an always-on helper". A browser opens Oracle's stack page with everything filled in.
2. Approve in Oracle's console.
3. Scan the QR code the app shows. The phone and the VM are paired.

Everything else (updates, backups, staying within the free allowance, rebuilding after loss) is the app's job, and the person never sees a terminal.

---

## 8. How this changes the build plan

| Was planned | Now recommended | Why |
|---|---|---|
| Port the router to Rust | **Stop after the first slice.** Keep the Python router as a local service on the desktop and the VM | Nothing the person sees depends on a Rust router, and LiteLLM is Python anyway |
| Rust core holds everything | Rust core holds vault, mandates (Biscuit), receipts, sync (Loro or Automerge) and the device link (iroh) | These are what must run inside the apps |
| Own mandate format | **Biscuit** | Exists, audited use elsewhere, narrows offline |
| Own sync | **Loro or Automerge over iroh** | Exists |
| VM reached by SSH tunnel | **iroh**, with QR pairing | No ports, no accounts |
| Chat-app interface as a late extra | **LINE reply-only bot early** | It is free in Taiwan and is how the first users already talk |
| Phones as full standalone helpers | **Phones as viewer and approver first**, working through the desktop or VM | iOS does not allow an always-on helper; see section 9 |
| Chain work in phase 6 | **Mandate compiler first, on the test network**, then payments | The mandate is the product's centre and needs no real money |
| Health as a parked add-on | Unchanged, but **start the two slow conversations now**: a Taiwan entity or hospital partner for My Health Bank, and a regulator's view on "explain my report" | Both take months and need no code |

Suggested order for the next twelve weeks:

1. **Weeks 1–2.** Harden the LiteLLM pin. Fix the VM size. Build the four first skills and run the real-allowance test. Recruit the 20 testers.
2. **Weeks 2–5.** Tauri desktop shell over the existing router: first-minute setup with OpenRouter and Hugging Face sign-in, the allowance screen, the chores screen.
3. **Weeks 3–6.** Mandates on Biscuit and receipts in the Rust core; the mandate screen; the sandbox for skills.
4. **Weeks 5–8.** One-button VM, iroh pairing, sync, the LINE reply bot.
5. **Weeks 7–10.** Mandate compiler to the test network: Spend Permission, x402 `exact` and `upto`, EAS anchoring.
6. **Weeks 9–12.** Phone viewer and approver. Credits as signed ledger entries between two test users.

---

## 9. Decisions this review asks the owner to revisit

1. **Phones.** The earlier decision was one core with both phones standalone. iOS background limits mean a phone cannot run chores unattended. Recommendation: phones approve and view first; standalone later, limited to work done while the app is open.
2. **Rust scope.** Recommendation: pause the router port; keep the conformance suite so it can resume.
3. **Company structure.** ⚖ A Taiwan branch or subsidiary helps twice: the My Health Bank SDK requires it, and it may ease the Singapore licensing question. Needs counsel.
4. **Wallet supplier.** Recommendation: Coinbase's stack for the test network because it is the least work, with the exits named in section 3.6.
5. **Publishing the catalogue.** No maintained machine-readable free-tier catalogue was found. Publishing Muse's, dated and sourced, would be useful to others and cheap to do.

---

## 10. Not verified in this review

- Whether pay-as-you-go Oracle accounts keep the old 4 / 24 allowance; what exactly happens to an over-size instance.
- Whether LiteLLM 1.108.5 is signed with the new key; the March 2026 incident details.
- USDC on Base accepting smart-wallet signatures for transfer authorisations; Permit2; in-app routes from Taiwan dollars or Singapore dollars to USDC.
- Audit status of EAS, Safe modules and ERC-8004; the content of MetaMask's audits; Semaphore's current version and Base deployment.
- Kernel/ZeroDev, Para, thirdweb, Superfluid, Circles v2's licence and parameters.
- Tailscale and Cloudflare Tunnel free-plan terms; home-server products; Telegram and WhatsApp pricing.
- Taiwan: the status of the new data-protection commission; the regulator's position on report-explaining apps; the Digital Credential Wallet's protocols; SNOMED licensing; the cloud provisions of the electronic-record rules; Enforcement Rules §49-1; 2025–26 changes to the research and biobank acts.
- Any reliable figure for what patients are paid for data or what buyers pay.
- Speed of local models on a 2 OCPU ARM VM (the figure given is an estimate).

---

## 11. Main sources

Opened on 2026-11-13 unless a section above marks the claim [S] or [U].

**Infrastructure**
- Oracle Always Free resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle deploy button: https://docs.oracle.com/iaas/Content/ResourceManager/Tasks/deploybutton.htm
- LiteLLM incident, 21 October 2026: https://docs.litellm.ai/blog/security-incident-oct-21-2026
- GitHub Models retirement: https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models
- OpenRouter limits: https://openrouter.ai/docs/api/reference/limits
- Hugging Face OAuth: https://huggingface.co/docs/hub/oauth
- Vercel AI Gateway pricing: https://vercel.com/docs/ai-gateway/pricing
- LINE Official Account prices in Taiwan: https://tw.linebiz.com/column/LINEOA-2026-Price-Plan/ and https://developers.line.biz/en/docs/messaging-api/pricing/
- MCP versioning: https://modelcontextprotocol.io/specification/versioning
- iroh 1.0: https://iroh.computer/blog/v1
- Malicious skills report: https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html
- Traditional Chinese benchmark (TMMLU+): https://arxiv.org/abs/2403.01858

**Chain and payments**
- x402: https://github.com/x402-foundation/x402 and https://docs.cdp.coinbase.com/x402/seller/facilitator.md
- AP2: https://ap2-protocol.org/
- Spend Permissions: https://github.com/coinbase/spend-permissions
- MetaMask Delegation Framework: https://github.com/MetaMask/delegation-framework
- Circle nanopayments: https://developers.circle.com/gateway/nanopayments.md
- Circle paymaster: https://developers.circle.com/paymaster/pay-gas-fees-usdc.md
- USDC addresses: https://developers.circle.com/stablecoins/usdc-contract-addresses
- EAS contracts: https://github.com/ethereum-attestation-service/eas-contracts
- ERC-8004: https://eips.ethereum.org/EIPS/eip-8004 and https://github.com/erc-8004/erc-8004-contracts
- 0xSplits v2: https://splits.org/protocol/docs/core/split-v2/
- Sablier Flow deployments: https://docs.sablier.com/guides/flow/deployments
- Semaphore: https://github.com/semaphore-protocol/semaphore
- Taiwan Virtual Asset Service Act: https://www.theblock.co/news/regulation/2026-07-01-taiwan-passes-crypto-law-406816
- Singapore licensing deadline (secondary): https://cms.law/en/sgp/legal-updates/singapore-s-june-30-hard-deadline-for-crypto-firms-serving-overseas-customers

**Health**
- TW Core: https://twcore.mohw.gov.tw/ig/twcore/
- Health ministry SMART on FHIR app call: https://mohw.gov.tw/cp-16-84204-1.html
- Medical Care Act: https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0020021
- National Health Insurance Data Management Act, third reading: https://www.mohw.gov.tw/cp-2704-84706-1.html
- My Health Bank SDK (secondary): https://www.ithome.com.tw/news/165575
- SMART Health Cards and Links: https://hl7.org/fhir/uv/smart-health-cards-and-links/STU1/history.html
- SMART App Launch: https://hl7.org/fhir/smart-app-launch/
- mCODE: https://www.hl7.org/fhir/us/mcode/
- Kill the Clipboard: https://vintasoftware.github.io/kill-the-clipboard/latest
- OpenSAFELY job log: https://docs.opensafely.org/jobs-site
- Discharge-summary study: https://jamanetwork.com/journals/jamanetworkopen/fullarticle/10.1001/jamanetworkopen.2024.0347
- Digital Credential Wallet (secondary): https://moda.gov.tw/en/press/press-releases/15752

The research was done by three assistants working from these and other pages. Only the Oracle, LiteLLM and GitHub Models pages were opened a second time to confirm their claims; the rest were read once.
