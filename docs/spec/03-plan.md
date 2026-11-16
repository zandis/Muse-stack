# Muse v1 specification, part 3: the build plan

Written 2026-11-13. Part 1 is the modules (`01-architecture.md`); part 2 is the experience (`02-ux.md`); part 4 (`04-execution-plan.md`) breaks these milestones into work packages with sizes, a critical path and gates, and corrects this part's twelve-week estimate. This replaces the phase list in `docs/muse-plan.md` §10. The two phase-0 tests there still decide whether the rest is worth building, and they are folded into milestone 1 below.

Assumed team, from the owner's earlier answer: the owner plus one or two engineers, twelve weeks. Work marked **(owner)** cannot be done by an engineer.

---

## 1. Where things stand

| Piece | State on `ccr-645e0264-bn082z` |
|---|---|
| Router: ledger, privacy classes, rationing, paid cap, pacing, routing rules, hand-off options, exact cache | Built, tested, run once against real providers |
| Queue | Built, tested |
| Catalogue with dated facts, `muse connect`, `muse catalog check` | Built, tested |
| Read-only dashboard | Built |
| Supply-chain pinning and `Retry-After` on provider 429 | In progress |
| Rust core, first slice (parsing, clocks, pacing, cache key) | Built; paused by this plan |
| Vault, mandates, receipts in Python (`muse/trust`) | Built, parked; to be replaced by the core |
| Clickable mock-up | Built; behind this specification |
| Helper, skills, sandbox, apps, sync, pairing, VM setup, payments, credits | Not started |

---

## 2. Milestones

Each milestone ends with something a tester can use and a short list of checks that must pass. Weeks overlap because the work splits between people.

### Milestone 1: prove the two risks (weeks 1–2)

**Goal.** Know whether non-technical people can connect, and whether chores succeed on free allowances.

| Work | Module | Done when |
|---|---|---|
| Pin LiteLLM by digest and hash; verification script | Router | The config check refuses an unpinned image; the script runs clean on the VM |
| `Retry-After` and a provider-limit header on provider 429 | Router | The three choices can show a real time |
| Output floor for reasoning models (`min_output_tokens`) | Router, Catalogue | A 16-token request to a reasoning model returns visible text or a clear refusal |
| Four first skills as plain Python on the router: set up my services, answer over my files, research report, recurring chore | Helper (first form) | Each has a check of its own output |
| Run each skill 50 times on real free allowances | **(owner)**, on the VM | At least 80% pass their own check with nothing spent |
| Update the mock-up to part 2 | Mock-up | All screens in `02-ux.md` §10 present |
| Twenty testers on the mock-up and the real one-tap connection | **(owner)** | 15 of 20 reach a first answer in 3 minutes; 10 of 20 add a second service unaided |
| Resize the VM to the free allowance; confirm it is not billed | **(owner)** | Oracle console shows 2 OCPU, 12 GB, no charges |

**If a test fails.** Connection test: rework first run before anything else. Chores test: narrow the skills, lead with hand-off and the spending limit.

### Milestone 2: a desktop app a non-technical person can install (weeks 2–5)

**Goal.** Install, connect, ask, and see allowances, with no terminal.

| Work | Module | Done when |
|---|---|---|
| Home control API over a local socket | Router | The app never shells out to `muse` |
| Tauri desktop shell: first run, Today, Ask, Services, Settings, tray gauge | App shell | Install to first answer in under a minute on a clean machine |
| One-tap connect (OpenRouter; Hugging Face after verification) and guided-key flow in the app | App shell, Catalogue | The measures in `02-ux.md` §9 for both flows |
| The three choices wired to the router's trailer | App shell, Router | Each button does what it says |
| Packaged home services: the app starts and stops the router and queue | App shell | No Python or Docker knowledge needed |
| Installers for Windows and macOS, signed | App shell | Installs without security warnings |
| Traditional Chinese and English | App shell | Every string in both |

### Milestone 3: permissions and records (weeks 3–6)

**Goal.** Everything the helper does is under a permission the person can read and stop, and is recorded.

| Work | Module | Done when |
|---|---|---|
| Core crate: vault (keystore, SQLCipher, recovery key) | Vault | Opens only with the device or the recovery key; survives a wrong PIN |
| Core crate: mandates on Biscuit (propose, issue, narrow, check, revoke) | Mandates | A narrowed token can never do more than its parent (property test) |
| Core crate: receipts (chain, verify, prove) | Receipts | A changed or removed record is detected |
| Conformance vectors for the three, so Python on the home and Rust in the app agree | Core | The existing conformance job covers them |
| Compiler, first two targets: router app entries and skill tokens | Mandate compiler | Stopping a permission removes the app entry and refuses the next tool call |
| Sandbox for tools on Linux and macOS | Skill sandbox | A tool cannot read a path or reach a host its token does not name (tests try) |
| Helper runtime replaces the plain-Python skills; confirm cards | Helper | No write happens without a card or a standing permission |
| Screens: Permissions, Records, Chores, cards | App shell | The flows in `02-ux.md` §7.2–7.3 |
| Migrate or retire `muse/trust` | Core | One implementation remains |

### Milestone 4: always on, and on the phone (weeks 5–8)

**Goal.** Chores run with the computer off; the phone shows and approves.

| Work | Module | Done when |
|---|---|---|
| Link (iroh) and QR pairing with a check phrase | Sync and Link | Two devices pair in under a minute; a wrong phrase aborts |
| Sync of settings, permissions, revocations, records, chores | Sync and Link | A permission stopped on one device stops on all within seconds when online, and at next contact when not |
| One-button VM stack (2 OCPU, 12 GB): home services, updates by digest, backups, idle-reclaim guard | Devices | A non-technical tester adds it in under 10 minutes with no terminal |
| Moving the home from desktop to VM and back | Devices | Keys move only inside the pairing channel |
| LINE reply bot; pushed messages counted as an allowance | Connectors | A card can be opened from the person's own LINE chat |
| Phone app (iOS and Android): Today, Ask, cards, Records, Permissions, Devices, pairing, recovery | App shell | Approve from the lock screen works on both |
| Files, mail (drafts) and calendar connectors | Connectors | Each runs in the sandbox under its own permission |
| "I lost this device" | Devices | The lost device can no longer approve or read new data |

### Milestone 5: paying and being paid, on a test network (weeks 7–10)

**Goal.** A spending permission is real: enforced on chain, visible in the app, stoppable in two taps.

| Work | Module | Done when |
|---|---|---|
| Wallet creation with a passkey; test-network header everywhere | Pay, App shell | No seed phrase is shown; the header cannot be dismissed |
| Compiler target: spending limit on chain, created and revoked with the permission | Mandate compiler, Pay | Stopping the permission makes the next payment fail on chain as well as in the client |
| x402 client with client-side budget checks; `exact` and `upto` | Pay | A payment over the limit is refused before anything is signed |
| x402 endpoint in front of one helper result | Pay | Another test user's helper can buy it |
| Record roots anchored (attestation on the test network, or a transparency log) | Receipts | A record can be proven to have existed at the anchor time |
| Early check: do smart-wallet signatures work for the transfer authorisation x402 uses? | Pay | Answered in week 7; if not, the helper's spending key is a plain key under the on-chain limit |
| Wallet screen | App shell | Matches `02-ux.md` §4.8 |

### Milestone 6: exchange between two people (weeks 9–12)

**Goal.** Two test users exchange spare local compute for credits under standing permissions.

| Work | Module | Done when |
|---|---|---|
| Credit ledger: created only by matching records from both sides; halves every 30 days; small overdraft | Credits | Balances on both sides agree after any order of sync |
| Helper-to-helper job under a standing permission | Helper, Mandates | Neither helper can exceed the permission the other person gave |
| Credits tab | App shell | Shows where each credit came from |
| Japanese and Korean, if Taiwan testing has concluded | App shell | — |
| Review against the measures in `02-ux.md` §9 | All | Each measure met or its miss explained |

---

## 3. Order of dependencies

```
Milestone 1 ─┬─► 2 (app) ──────────────┬─► 4 (always on, phone) ─► 6 (exchange)
             └─► 3 (permissions) ──────┴─► 5 (paying)
```

- 2 and 3 run in parallel: one person on the app, one on the core.
- 4 needs the core (pairing uses the vault) and the app.
- 5 needs the compiler from 3 and the app from 2. It does not need 4.
- 6 needs 4 (two homes that can reach each other) and the records from 3.

If time runs short, cut in this order: Japanese and Korean; mail and calendar connectors; the x402 selling endpoint; credits (milestone 6); Android or iOS, whichever the testers use less.

---

## 4. Work that needs no code and should start now (owner)

| Item | Why now |
|---|---|
| Recruit the twenty testers | Slowest item; gates milestone 2's design |
| Singapore and Taiwan opinions on tokens and licensing ⚖ | Gates any mainnet step; shapes company structure |
| Decide on a Taiwan branch or subsidiary ⚖ | Needed for the My Health Bank SDK; may ease licensing |
| Ask the regulator about "explain my report" ⚖ | Gates the health add-on; takes months |
| Find one hospital willing to pilot patient-scoped access in 2027 | Same |
| Apple and Google developer accounts; code-signing certificates | Needed for milestone 2 installers and milestone 4 phones |
| A LINE official account for the bot | Needed for milestone 4 |

---

## 5. How to go faster

1. **Run tests where the keys are.** Install Claude Code on the VM with Remote Control so real-provider tests no longer pass through the owner.
2. **Merge as work lands.** Decide PRs #1 to #4 in one sitting; merge #5. Stacked open branches slow every review.
3. **Run CI on the work branch.** The workflow triggers only on pull requests and `main`, so direct pushes are untested by CI. Either open a pull request per change or add the branch to the trigger.
4. **Reuse before writing.** Biscuit, iroh, Loro or Automerge, x402, Spend Permissions and the skills format each replace weeks of work. Wrap them; do not fork them.
5. **Keep the router in Python.** The conformance suite stays, so a port can resume if a phone ever needs the router on board.
6. **One screen at a time against the measures.** Each screen in part 2 has a number to hit; stop polishing when it is hit.

---

## 6. Risks to the plan

| Risk | Sign | Response |
|---|---|---|
| Non-technical people do not finish guided sign-up | Fewer than 10 of 20 add a second service | Lead with one-tap services; consider a small starter allowance, which the owner declined once |
| Chores fail on small free models | Under 80% pass | Narrow skills; hand-off earlier; test Traditional Chinese quality with a local set |
| A free allowance changes or disappears | The weekly catalogue check flags a page | Edit the entry; the app shows the new figure with its date |
| Another compromised dependency | An advisory, or the verification script fails | Pins hold the old version; upgrade deliberately |
| Oracle reclaims or closes the VM | The always-on helper goes unreachable | The desktop takes over; rebuild from backup in one tap |
| iOS limits break approvals or pairing | Testing on real devices in milestone 4 | Approvals fall back to opening the app |
| Smart-wallet signatures do not work with x402 transfers | The week-7 check fails | Plain spending key under the on-chain limit |
| Licensing ⚖ | Counsel's opinion | Stay on the test network; no redemption; restructure if advised |
| Two engineers is too few for six milestones | Milestone 2 or 3 slips past week 6 | Apply the cut order in section 3 |

---

## 7. Decisions needed from the owner before milestone 2 starts

1. ~~Confirm the four assumptions at the top of part 1.~~ Confirmed by the owner on 2026-11-13.
2. Which phone platform first, if not both.
3. Default daily spending limit shown to new users (the specification uses NT$30).
4. Whether to publish the catalogue openly.
5. Whether the first testers get the always-on helper flow or only the desktop app.
