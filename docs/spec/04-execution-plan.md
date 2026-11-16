# Wardmuse v1 specification, part 4: the plan to completion

Written 2026-11-13. Parts 1 to 3 say what is built and in what order. This part says how the work is run so that it finishes: the tracks, the critical path, every work package with its size and dependencies, the gates, and who does what.

"Completion" here means **v1 as specified in parts 1 and 2**: a signed desktop app and phone apps that a non-technical person in Taiwan can install; the helper doing chores on free allowances; permissions and records; the always-on helper; payments and credits on a test network. It does not include mainnet, a token, real health data, or a public skill marketplace.

## Revision, 2026-11-13: prove before widening

After the first real runs and the review in `docs/reviews/status-review-2026-11-13.md`, the owner asked for everything that improves the project to be done. The working plan is therefore the review's recommendations, until the owner says otherwise:

1. **No new modules.** The four packages in flight finish (research-skill fixes, permission enforcement in "advise" mode, the app's first screens, device pairing as an unwired prototype). Nothing else starts until gate 1 is settled.
2. **Gate 1 is judged on "right by expected facts"**, not on a skill's own check. Measured on 133 real runs: answer over my files 65 of 67 (passes); research report 27 of 66 (fails) while its own check passed 54. See `docs/reviews/gate1-full-2026-11-13.md`.
3. **The first release is smaller**: router with rationing and a hard daily limit; answer over my files; chores from files; set up my services; a desktop app with Today, Ask, Services, Chores; the host adapter; records. Research reports, phones, pairing, the always-on helper, wallet, credits, third-party skills and health wait.
4. **Real runs are routine**: a capped nightly run on the VM (`docs/vm-test-plan.md`), and CI on every push to the work branch.
5. **Unreviewed security code holds nothing real.** Vault, permission tokens, pairing and sandbox stay prototypes (permissions in "advise" mode) until an outside review.

The package table and calendar below are kept as the full plan; this revision says which part of it is being built now.

## 0. Decisions now fixed

The owner confirmed on 2026-11-13 ("as your advice"):

| Decision | Outcome |
|---|---|
| Router | Stays in Python as a local service. The Rust router port stops after its first slice; the conformance suite is kept |
| Phones | Viewer and approver first. No unattended chores on a phone in v1 |
| Permissions | Biscuit tokens |
| Chain | Coinbase's stack on Base Sepolia, with the exits named in part 1 §2.12 |

Also confirmed by the owner on 2026-11-13:

| Decision | Outcome |
|---|---|
| Phone platforms | Both iOS and Android |
| Default daily spending limit | NT$30 |
| Catalogue | Published openly as dated JSON |
| First testers | Get the always-on helper flow as well as the desktop app |
| Phone push | A minimal relay that forwards an empty wake signal and never sees content. It is the only service Wardmuse runs |

Decided on 2026-11-15:

| Decision | Outcome |
|---|---|
| Vault slot for the always-on VM | None by default. The VM holds only what a job is handed; the person can add a slot explicitly later |
| Recovery words | Shown once, at creation; never shown again. Lost words mean a new recovery key from an unlocked device |
| VM idle guard | On by default for Always Free accounts, off for paid ones (`deploy/oracle-stack/README.md`, "Idle guard") |

**The owner's VM.** The owner reports 4 cores and 48 GB. Oracle's Always Free allowance is 2 cores and 12 GB, so this instance is above it. Either it is on a paid account and is being charged for the difference (a rough estimate from Oracle's public list prices is US$50 to 60 a month; check the console's cost page, this figure is not verified), or it is on a free-only account and may be stopped. It is a good test bench either way. The product must still be built and measured for 2 cores and 12 GB, because that is what a user gets for free; the VM stack in work package D3 is tested at that size.

---

## 1. What makes this plan faster than part 3's first draft

1. **Risk first.** The two things that can kill the product (people cannot connect; chores fail on free allowances) are answered in the first two weeks, before any app code that depends on the answer.
2. **Vertical slices.** Every two weeks ends with something a tester can touch, not a layer. The first slice is deliberately thin: one skill, one screen, one service.
3. **Five tracks in parallel**, most of them carried by Claude sessions, with interfaces frozen early so tracks do not wait on each other (section 3).
4. **Contract tests at every boundary.** The home control API, the permission token, the record format and the sync documents each get a conformance suite, as the router already has. Two people, or two Claude sessions, can then build either side independently.
5. **Reuse is a rule.** A work package may not write what part 1 names as reused (Biscuit, iroh, Loro or Automerge, x402, Spend Permissions, the skills format, SQLCipher, age) without the owner's sign-off.
6. **Tests where the keys are.** A Claude Code session on the VM runs real-provider checks nightly, so the owner stops relaying results.
7. **Slow, non-code items start on day 1** (section 7). Several have lead times longer than any code.

---

## 2. Who does what

| Role | Does | Does not |
|---|---|---|
| **Owner** | Decisions at gates; tester recruitment; accounts, certificates and legal; reviews pull requests; reads the zh-TW wording | Relay test output by hand |
| **Engineer A** (app) | Track B (app shell), then phones | |
| **Engineer B** (core) | Track C (core), then sync and pairing | |
| **Claude sessions** | Work packages end to end with tests; reviews; research; the nightly real-key run on the VM | Merge, release, spend real money, touch real health data |

If there is one engineer or none, the same plan holds with Claude sessions taking a track each and the owner reviewing; section 9 gives the stretched calendar. A Claude session works one package at a time on its own branch and opens a pull request; nothing lands without the suite green.

**Working rules.**
- One pull request per work package, against `ccr-645e0264-bn082z` until `main` is chosen as the trunk (WP A0).
- Definition of done for every package: tests written first for the acceptance check; full suite green; documentation updated; no new unpinned dependency; a note of what was not verified.
- A weekly 30-minute review with the owner: what shipped, what a tester saw, the next gate.

---

## 3. Tracks and frozen interfaces

| Track | Contents |
|---|---|
| **A. Home** | Router, catalogue, queue, control API, helper runtime, skills, sandbox, connectors |
| **B. App** | Tauri desktop shell, screens, installers, then iOS and Android |
| **C. Core** | Vault, permissions, records (Rust), their Python twins on the home, the compiler |
| **D. Always-on** | Link, pairing, sync, VM stack, LINE |
| **E. Pay** | Wallet, spending limit, x402, anchoring, credits |
| **O. Owner** | Testers, legal, accounts, hospital conversations |

Interfaces to freeze, so tracks can work apart:

| Interface | Between | Frozen by | Form |
|---|---|---|---|
| Home control API | A ↔ B | End of week 2 | OpenAPI file plus a fake home the app develops against |
| Permission token | C ↔ A, D, E | End of week 3 | Biscuit schema: facts, checks, the narrowing rules; test vectors |
| Record format | C ↔ A, B | End of week 3 | Canonical JSON, signature, chain rule; test vectors |
| Sync documents | C ↔ D, B | End of week 5 | Document list and merge rule per document |
| Card format | A ↔ B, D | End of week 2 | One JSON shape for every card in part 2 §5 |
| Compiler targets | C ↔ A, E | End of week 4 | Per target: create, status, revoke |

A change to a frozen interface needs both tracks' agreement and a version bump.

---

## 4. Work packages

Sizes are working days for one person or one Claude session, including tests. "Needs" lists what must be finished first. Packages with no shared "Needs" can run at the same time.

### Track A: Home

| ID | Package | Size | Needs | Acceptance |
|---|---|---|---|---|
| A0 | Trunk and CI: pick the trunk branch, run CI on it, close or merge PRs #1 to #5 | 1 | Owner | Every push is tested by CI, including the Rust job |
| A1 | Output floor for reasoning models (`min_output_tokens` in the catalogue; router raises and reports) | 2 | — | A 16-token request to a reasoning model returns text or a clear refusal |
| A2 | Home control API v1 over a local socket, from the existing dashboard server; OpenAPI file; fake home | 4 | — | The dashboard and a test client use only this API |
| A3 | Card format and pending-approval store | 2 | A2 | A card created on the home is listed, answered and expires |
| A4 | Skill format loader (`SKILL.md`), skill checks, budgets | 3 | — | A skill declares its tier, budget and output check; a failed check retries once on a stronger allowance |
| A5 | Skill: answer over my files (index, retrieve, cite) | 5 | A4 | Every claim cites a passage; passes its check on small free models |
| A6 | Skill: set up my services (ranked suggestion, guided steps from the catalogue) | 3 | A4 | Suggests the next service by allowance gained per minute |
| A7 | Skill: recurring chore (sentence to schedule, spare-allowance timing) | 4 | A4, A3 | "Every Monday summarise these feeds" becomes a job with a budget |
| A8 | Skill: research report (many light steps, one heavy step, hand-off offered) | 5 | A4 | The heavy step offers the three choices when the strong allowance is used up |
| A9 | Real-allowance test harness: 50 runs per skill, pass rate per allowance, nothing spent | 2 | A5–A8 | A report the owner can read; run nightly on the VM |
| A10 | zh-TW evaluation set (200 short tasks) and ranking of free models on it | 3 | — | The rules file's defaults for Taiwan are chosen from data |
| A11 | Helper runtime: jobs, steps, confirm-before-write, receipts per step | 6 | A3, C4, C6 | No write happens without a card or a standing permission |
| A12 | Sandbox on Linux and macOS; token verified by the sandbox | 6 | C4 | A tool cannot read a path or reach a host its token does not name |
| A13 | Sandbox on Windows | 4 | A12 | Same tests pass on Windows |
| A14 | Connectors: files; mail drafts; calendar | 6 | A12 | Each runs sandboxed under its own permission |
| A15 | Catalogue: publish as JSON; weekly page check on the home; Hugging Face and LINE entries after verification | 3 | — | A changed source page appears in the maintainer's review list |
| A16 | Packaging the home: one self-contained bundle the app starts and stops (no Python or Docker knowledge) | 5 | A2 | Starts on a clean Windows and macOS machine |

### Track B: App

| ID | Package | Size | Needs | Acceptance |
|---|---|---|---|---|
| B1 | Tauri shell, design tokens from the mock-up, zh-TW and English strings, the fake home | 4 | A2 (file) | Runs against the fake home |
| B2 | First run, including one-tap connect and the recovery-key step | 5 | B1, C2 | Install to first answer under 1 minute on a clean machine |
| B3 | Today and tray gauge | 3 | B1 | Matches part 2 §4.1 and its states |
| B4 | Ask, with the three choices and the hand-off | 4 | B1 | Each choice does what it says |
| B5 | Services, with guided-key flow | 5 | B1, A6 | Guided key under 3 minutes in the tester study |
| B6 | Cards, Chores | 4 | B1, A3, A7 | A chore is created by one sentence and one confirmation |
| B7 | Permissions, Records | 5 | B1, C4, C6, C7 | Stop takes two taps and shows each target turning off |
| B8 | Settings, For apps | 3 | B1 | Another program can be given the endpoint with its own key |
| B9 | Installers, signing, auto-update, the supply-chain check in About | 5 | A16, Owner (certificates) | Installs without security warnings; updates verify signatures |
| B10 | Devices screen and pairing UI | 4 | D2 | Pair in under a minute |
| B11 | Phone app, iOS: Today, Ask, cards, Records, Permissions, Devices, recovery, push | 10 | D3, C1–C6 on iOS | Approve from the lock screen |
| B12 | Phone app, Android: the same | 7 | B11 | Same |
| B13 | Wallet screen and Credits tab | 4 | E1, E6 | Matches part 2 §4.8; the test-network header cannot be dismissed |
| B14 | Accessibility pass (screen reader, contrast, large text) and native zh-TW copy review | 4 | B2–B8, Owner | Every flow works with a screen reader; copy signed off by a native reader |

### Track C: Core

| ID | Package | Size | Needs | Acceptance |
|---|---|---|---|---|
| C1 | Core crate layout; build for desktop, iOS, Android; Tauri commands | 3 | — | One crate builds for all three |
| C2 | Vault: keystore wrap, SQLCipher store, per-record encryption, recovery key, rotate | 8 | C1 | Opens only with the device or the recovery key |
| C3 | Permission schema on Biscuit; test vectors | 3 | — | Vectors agreed by tracks A, D, E |
| C4 | Permissions: propose, issue, narrow, check, revoke; revocation list | 6 | C1, C3 | Property test: a narrowed token never does more than its parent |
| C5 | Record format and vectors | 2 | — | Vectors agreed by tracks A, B |
| C6 | Records: chain, verify, prove, export | 4 | C1, C5 | A changed or removed record is detected |
| C7 | Compiler, targets 1 and 2: router app entries and skill tokens; nightly orphan sweep | 5 | C4, A2 | Stopping a permission removes the app entry and refuses the next tool call |
| C8 | Python twins of C4 and C6 for the home, held to the same vectors | 4 | C3, C5 | The conformance job covers both languages |
| C9 | Retire or migrate `muse/trust` | 2 | C2, C4, C6 | One implementation remains |
| C10 | Passkey unlock (PRF) as an optional second way in | 3 | C2 | Works where the platform supports it; never the only way in |

### Track D: Always-on

| ID | Package | Size | Needs | Acceptance |
|---|---|---|---|---|
| D1 | Link: iroh in the core; connect two devices by key | 4 | C1 | Connects through NAT with no open port |
| D2 | Pairing: QR, one-time secret, four-word check, key hand-over | 4 | D1, C2 | A wrong phrase aborts; keys never leave the channel |
| D3 | Sync: documents, merge rules, encryption before sending | 8 | D1, C2, sync freeze | A permission stopped on one device stops on all |
| D4 | VM stack: deploy button, cloud-init, services by digest, updates, backups, idle guard, at 2 cores and 12 GB | 6 | A16 | A non-technical tester adds it in under 10 minutes, no terminal |
| D5 | Moving the home between desktop and VM | 3 | D2, D3, D4 | Keys move only inside the pairing channel |
| D6 | LINE reply bot; pushed messages as an allowance; public webhook via a tunnel | 5 | A3, Owner (LINE account) | A card opens from the person's own chat |
| D7 | Lost device: revoke, rotate, re-wrap | 3 | D3, C2 | The lost device cannot approve or read new data |
| D8 | Push notifications for cards (iOS, Android) without a Wardmuse server holding content | 4 | D3 | The push carries no private text; the app fetches the card over the Link |

D8 uses a minimal relay that forwards an empty "wake" signal only; it never sees content (confirmed by the owner). It is the one exception to "no Wardmuse-run server" in part 1 §5 and §7, and must be stateless, hold no account data, and be replaceable by fetching approvals when the app is opened.

### Track E: Pay (test network)

| ID | Package | Size | Needs | Acceptance |
|---|---|---|---|---|
| E0 | Spike: do smart-wallet signatures work for the transfer authorisation x402 uses on Base Sepolia? | 2 | — | A yes or no in week 7 at the latest; decides the helper's key type |
| E1 | Wallet creation with a passkey; test-network header | 4 | E0 | No seed phrase shown |
| E2 | Compiler target 3: spending limit on chain, created and revoked with the permission | 5 | C7, E1 | Stopping the permission makes the next payment fail on chain as well |
| E3 | x402 client with client-side budget checks; `exact` and `upto` | 5 | E1, C4 | A payment over the limit is refused before signing |
| E4 | Record roots anchored: attestation on the test network, and a transparency log | 4 | C6 | A record is proven to have existed at the anchor time |
| E5 | x402 endpoint in front of one helper result | 3 | E3 | Another test user's helper buys it |
| E6 | Credits ledger: two-sided records, 30-day halving, overdraft | 6 | C6, D3 | Balances agree after any order of sync |
| E7 | Helper-to-helper job under a standing permission (spare local compute) | 5 | E6, A11, D3 | Neither helper can exceed the other person's permission |

### Totals

| Track | Days |
|---|---|
| A. Home | 64 |
| B. App | 67 |
| C. Core | 40 |
| D. Always-on | 37 |
| E. Pay | 34 |
| **All** | **242** |

Add 20% for integration and fixes found by testers: about **290 working days**.

---

## 5. Critical path and calendar

The longest chain of dependencies decides the end date:

```
C1 → C2 → D1 → D2 → D3 → B11 (iOS) → B12 (Android) → G4
 3     8     4     4     8      10           7        = 44 days, about 9 weeks
```

Everything else has slack if started on time. The second-longest chain is `C3 → C4 → A12 → A14` and `A11` (the helper under permissions), about 6 weeks. So the core crate and the vault start on day 1, in parallel with the risk tests.

How long it takes depends on how many tracks are built at once. 290 working days divided among builders gives:

| Builders working in parallel | Calendar |
|---|---|
| Five (one per track: for example two engineers and three Claude sessions) | 12 to 14 weeks; the limit is the 9-week critical path plus integration |
| Three | About 20 weeks |
| Two | About 29 weeks |

So the earlier target of twelve weeks with the owner and one or two engineers is not reachable by those people alone. It is reachable only if Claude sessions carry the remaining tracks, and then the limit becomes how fast the owner and engineers can review: about five pull requests a week per track. The calendar below assumes five tracks.

| Weeks | A. Home | B. App | C. Core | D. Always-on | E. Pay | O. Owner |
|---|---|---|---|---|---|---|
| 1–2 | A0, A1, A2, A3, A4, A5, A6 | B1 | C1, C3, C5, C2 starts | — | — | Testers, accounts, counsel, VM size |
| 3–4 | A7, A8, A9, A10, A16 | B2, B3, B4 | C2, C4, C6, C8 | D1 | E0 | Tester study 1 |
| 5–6 | A11, A12, A15 | B5, B6, B8, B9 | C7, C9, C10 | D2, D3 starts | — | Sign installers; review copy |
| 7–8 | A13, A14 | B7, B10, B14 | — | D3, D4, D5, D6 | E1, E2, E3 | Tester study 2 |
| 9–10 | Fixes | B11 | — | D7, D8 | E4, E5 | LINE account live |
| 11–12 | Fixes | B12, B13 | — | Fixes | E6, E7 | Tester study 3 |
| 13–14 | Hardening, release | Release | — | — | — | Go or no-go |

---

## 6. Gates

A gate is a decision with evidence. Work past a gate does not start until it is passed or the owner waives it in writing.

| Gate | When | Evidence | If it fails |
|---|---|---|---|
| **G1: the product can work** | End of week 4 | A9 report: at least 80% of runs are right by expected facts (not merely passing their own check) on free allowances with nothing spent. Tester study 1: 15 of 20 reach a first answer within 3 minutes on the mock-up and the real one-tap connection; 10 of 20 add a second service unaided | Chores fail: narrow the skills, lead with hand-off and the limit. Connection fails: rework first run before B5; revisit a starter allowance |
| **G2: desktop alpha** | End of week 6 | Clean-machine install to first answer under 1 minute; signed installers; A1 to A9 and B1 to B6, B8, B9 done | Slip phones, not the desktop |
| **G3: permissions are real** | End of week 8 | Stopping a permission stops the app entry, the tool call and (on the test network) the spending limit; records verify; sandbox escape tests pass; an outside security review of C2, C4, A12 is booked | No connector that writes ships until it passes |
| **G4: always on, and on the phone** | End of week 12 | VM added by a non-technical tester in under 10 minutes; pairing under a minute; approve from the lock screen on iOS and Android; lost-device flow works | Ship desktop and VM; phones follow |
| **G5: v1 complete** | End of week 14 | Every measure in part 2 §9 met or its miss explained; the security review's findings closed or accepted; 20 testers have used it for a week; the owner's go | Hold the release; the list of misses is the plan |

Kill criteria carried from the earlier plan: if G1 fails twice after rework, stop and reconsider the product before building apps.

---

## 7. Owner's track, by lead time

| Start | Item | Lead time | Blocks |
|---|---|---|---|
| Day 1 | Check the VM's cost in Oracle's console; decide to keep it as a paid test bench or resize | Minutes | Unexpected charges or a shutdown |
| Day 1 | Recruit 20 non-technical testers in Taiwan | 2–3 weeks | G1 |
| Day 1 | Apple and Google developer accounts; Windows and macOS code-signing certificates | 1–3 weeks | B9, B11, B12 |
| Day 1 | Install Claude Code on the VM with Remote Control | 30 minutes | A9 nightly runs |
| Week 1 | Counsel in Singapore and Taiwan on tokens, licensing, and company structure ⚖ | 4–8 weeks | Anything beyond the test network |
| Week 1 | Decide on a Taiwan branch or subsidiary ⚖ | Months | My Health Bank SDK; possibly licensing |
| Week 2 | LINE official account for the bot | Days | D6 |
| Week 2 | A native zh-TW reader for the copy | — | B14 |
| Week 4 | Book an outside security review of the vault, permissions and sandbox | 4–6 weeks to schedule | G3, G5 |
| Week 4 | Ask the regulator about "explain my report"; approach one hospital about a 2027 pilot ⚖ | Months | The health add-on, which is outside v1 |

---

## 8. Quality and release

- **Test layers.** Unit tests per module; conformance vectors at each frozen interface; the real LiteLLM proxy against a fake upstream (exists); the app against the fake home; end to end on a clean machine in CI for Windows, macOS and Linux; the nightly real-key run on the VM.
- **Security.** Dependencies pinned by hash and digest (done for LiteLLM; extended to the Rust and JavaScript lock files in B9 and C1). No secret in logs, snapshots or crash reports, checked by a canary test as the real-key script already does. The outside review before G5.
- **Release channels.** Nightly for the team; weekly alpha for testers from G2; a beta at G4; v1 at G5. Updates are signed and verified by the app.
- **Telemetry.** None leaves the device by default. Timing measures for part 2 §9 are kept locally and shared only when a tester chooses to.
- **Rollback.** The home's data formats carry a version; an update that changes one ships a migration and keeps a backup for one release.

---

## 9. If the team is smaller or time is shorter

| Situation | Calendar | What changes |
|---|---|---|
| Five tracks at once (as planned) | 12–14 weeks | — |
| Three builders | About 20 weeks | Track E starts after G3; one phone platform at G4; gates move out in proportion |
| Two builders | About 29 weeks, or 16–18 with the cuts below | Drop E6 and E7, one phone platform, files connector only |
| Claude sessions only, owner reviewing | 14–18 weeks | Limited by the owner's review time, not building; batch reviews twice a week; keep packages under 5 days so each review is small |
| Must ship something in 6 weeks | 6 weeks | Desktop alpha only: tracks A and B to G2, with permissions as a read-only preview |

Cut order when something slips, least painful first: Japanese and Korean; mail and calendar connectors (A14 keeps files only); the selling endpoint (E5); credits and exchange (E6, E7); the second phone platform (B12); Windows sandbox (A13, Windows runs skills without third-party tools until it lands).

---

## 10. The next ten working days

| Day | Home (A) | App (B) | Core (C) | Owner |
|---|---|---|---|---|
| 1 | A0 trunk and CI; A1 starts | B1 starts from the mock-up's tokens | C1 crate layout | VM cost check; start recruiting; order certificates; Claude Code on the VM |
| 2 | A1 done; A2 starts | B1 | C3 permission schema | Counsel briefed |
| 3 | A2 | B1 | C5 record format; C2 starts | — |
| 4 | A2; A4 starts | B1 done against the fake home | C2 | — |
| 5 | A2 done, API frozen; A3 | B3 starts | C2 | Weekly review 1 |
| 6 | A4 done; A5 starts | B3 | C2 | LINE account |
| 7 | A5; A6 starts | B3 done; B4 starts | C2; C4 starts | — |
| 8 | A5; A6 done | B4 | C4 | — |
| 9 | A5 done; A7 starts | B4 | C4; token frozen | — |
| 10 | A7; A9 harness started on the VM | B4 done; B2 starts | C6 starts | Weekly review 2; tester study 1 scheduled |

What Claude starts immediately, without waiting on anything: **A1** (output floor), **A2** (control API), **A4** then **A5** (the first skill), and **C3/C5** (the two formats and their vectors).
