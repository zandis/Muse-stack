# Muse: infra-first plan

2026-11-12. Written from the product owner's answers to the clarification questionnaire. It replaces the medical-first framing of the earlier design plans; those remain as reference for step 4's medical add-on only.

## 1. What Muse is

**One rationed endpoint for all of a person's AI apps, built from the free and low-cost services they already qualify for, with a bot on top.**

The order of work, fixed by the owner:

1. **Infra.** Find, track and ration free and low-cost resources.
2. **A good app, assisted by a bot.**
3. **Other jobs and resource exchange.**
4. **Data sales, decentralized.** Non-medical first. Medical is an add-on with its own entity and rules.

## 2. Decisions

| Area | Decision |
|---|---|
| First user | Any individual, non-technical |
| Platform | Desktop app, everything local; chat apps as a second interface |
| Access to services | Guided sign-up; the user's own accounts and keys |
| Languages | Traditional Chinese, English, Japanese, Korean |
| Licence | Open core, paid extras |
| Resources tracked | AI models and services, storage, compute and hosting, blockchain and transactions |
| Resources rationed automatically at launch | AI only; the rest are tracked and shown |
| Catalogue | Central, kept current by a bot, changes approved by a person, every entry dated |
| Add-ons | Model endpoints (including Hugging Face Spaces), MCP tools, skills, non-AI services, drives, communications, smart contracts |
| Routing | Readable fixed rules |
| Priorities | 1. Spend nothing unless approved. 2. Never interrupt the active app. 3. Use free quota before it resets. |
| Paid fallback | Automatic up to a daily cap (default US$1) |
| Consumer subscriptions | Excluded |
| Providers hosted in mainland China | Allowed, labelled, never used for private requests |
| Default privacy class | Standard |
| Bot, first version | Answer over the user's files; research reports; recurring chores; set up the user's free services |
| Connectors | Cloud drives, email and calendar, chat apps |
| Bot autonomy | Reads freely; every write, send or purchase needs confirmation |
| Trust layer | Vault, mandates and receipts for everything |
| Memory | Local, editable by the user |
| Jobs | Bot services to other agents, paid human tasks, verified machine work, sharing and exchanging spare resources |
| Capacity for jobs | The user's own hardware and paid keys only |
| Exchange accounting | Non-transferable credits; a token later, as tokenised credits |
| Real money in v1 | None. Credits and test networks only. |
| Payout, when enabled | User chooses; bank transfer by default |
| Revenue | Free router, paid features, a cut on earnings |
| Data for sale | Usage statistics, ratings and preferences, expert judgments, own documents |
| Decentralized means | Company holds no data or keys; consent verifiable by anyone; payments bypass the company; user governance |
| Blockchain's roles | Payment rail, proof layer, a resource to ration |
| Expected buyers | AI labs and model providers; researchers |
| Medical | Last; steward model with its own entity |
| Company | Singapore; Taiwan users first |
| Constraint | Free for users |
| Seed proof | Router and bot with real users |
| 90-day number | Free-tier services connected per user |
| Time and team | Three months; the owner plus one or two engineers |

## 3. What exists today

On branch `ccr-645e0264-bn082z`:

- **Router and resource manager.** Key store, quota and reset tracking, cross-provider fallback, blocks shared across aliases, privacy classes that fail closed, local models, usage ledger, CLI, dashboard. Tested against a real proxy with fake providers. Never run with real keys.
- **Trust layer** (`muse/trust`). Encrypted vault, mandates, receipts, and a gateway client that refuses unverified routes.
- **Rationing layer.** In progress: per-app keys and budgets, daily paid cap, background work yielding to the active app.

Parked on `health-assistant-pack`: the health assistant and its web app.

Not started: the desktop app, guided sign-up, the general bot, connectors, the catalogue beyond AI, the discovery bot, credits and exchange, add-on loading, Japanese and Korean.

## 4. Scope against time

The decisions above do not all fit in three months with one or two engineers. This is the phasing I recommend. It keeps every decision; it only orders them.

| Phase | Weeks | Delivers | Why this order |
|---|---|---|---|
| **1. Router that rations** | 1–3 | Per-app keys and budgets, paid cap, background queue, real-key test checklist, routing rules file | It is the product, and everything else calls it |
| **2. Guided setup + desktop shell** | 2–6 | Installable desktop app (Traditional Chinese and English); the bot walks the user through each free service; menu-bar glance | The 90-day number is services connected per user; this is what moves it |
| **3. Catalogue beyond AI** | 4–8 | Storage, compute and chain allowances tracked and shown; discovery bot proposing catalogue changes for approval | Tracking is cheap once the catalogue schema is general; no automation risk |
| **4. General bot** | 5–10 | Answer over files, research reports, recurring chores, all through the vault, mandates and receipts; cloud drives first | Needs phases 1–2; drives before email because read-only file access is the lowest-risk connector |
| **5. Credits and exchange (testnet)** | 6–12 | Credits ledger; one job type end to end; consent and receipts anchored on a test network | Built alongside, as decided, but only one job type until there are users to trade with |
| **Later** | after 12 | Japanese and Korean; email and calendar; chat-app interface; more job types; data sales; real money; token; medical | Each needs users, or a licence, or both |

Three things I would cut from the first three months if time runs short, in this order: the chat-app interface, email and calendar, the second job type.

## 5. How each step is built

### Step 1: infra

- **Catalogue.** One open file per resource type with the same shape: provider, what is free, limits and windows, reset rule, how to read remaining allowance, terms that matter (training on inputs, commercial use, region), date checked. The AI catalogue exists; storage, compute and chains reuse the schema.
- **Discovery bot.** Runs on a schedule on spare quota. Searches for new and changed free tiers, reads the provider's own pricing and terms pages, and opens a proposed catalogue change with sources. A person approves. Nothing reaches users unverified.
- **Rationing.** Each application gets its own key, a priority (interactive, normal, background) and optional daily limits. Background work yields to whatever the user is actively using, keeps a reserve of each quota for interactive use, and is let through when a quota is about to reset. Paid services are used only within the daily cap, and only when their price is known.
- **Add-ons.** An add-on declares what it brings (an endpoint, MCP tools, a skill, a non-AI service), which resources it consumes, and which privacy classes it may see. The router meters add-ons like any other application.

### Step 2: app and bot

- **Guided setup.** For each service: open the provider's sign-up page, tell the user what to click, capture the key, test it, and show what was gained. The bot does this as its first task.
- **Bot.** Works only through the trust layer: data in the vault, every action under a mandate, every action receipted. Reads freely; proposes writes; the user confirms.
- **Desktop shell.** A window (bot, resources, activity) and a menu-bar icon showing what is available now and when quotas reset.

### Step 3: jobs and exchange

- **Credits.** Contributing a resource the user genuinely owns earns non-transferable credits; using someone else's spends them. Free tiers and subscriptions cannot be contributed, because providers' terms forbid it.
- **First job type.** To be chosen in phase 5 from: paid human tasks (buyers exist today), bot services to other agents, verified machine work, spare local compute.
- **No real money in v1.** Cash-out, stablecoin settlement and any token come later, behind legal advice and a licensed partner.

### Step 4: data sales

- Same consent mechanism as the bot's mandates: scope, purpose, term, revocable, signed.
- Order: usage statistics, ratings and preferences, expert judgments, own documents.
- Chat logs are not offered: free text cannot be anonymised and contains other people's information.
- Medical: steward organisation per country, compute inside a protected environment, results only. Detailed in the earlier review.

## 6. Rules that keep it clean

From the legal check (US case law verified; other jurisdictions from secondary sources; not legal advice):

- One account and one key per provider. No rotation, no pooling between people, no consumer-subscription logins.
- Honour every rate limit and ban. Never route around a refusal.
- Truthful client identity.
- Describe the product as a quota-aware router for the user's own accounts. Never as a way to bypass limits.
- The company holds no keys, data or funds.

## 7. Open risks

| Risk | Why it matters | What reduces it |
|---|---|---|
| Never tested with real providers | The 429 formats for Gemini, OpenRouter and Cohere are parsed from community reports | Owner runs the real-key checklist in phase 1 |
| Guided sign-up breaks when providers change their pages | It is the path to the 90-day number | Keep steps as data in the catalogue, checked by the discovery bot |
| Non-technical users on a desktop app with local models | Many machines cannot run a useful local model | Cloud free tiers are the default path; local is optional |
| Free tiers shrink or add terms against aggregation | The whole value rests on them | Many providers; catalogue dated and re-checked; low-cost paid tier under the cap |
| No identified buyer for jobs or data | Steps 3 and 4 have no demand evidence | One job type only until users exist; buyer interviews before building sales |
| Scope | Section 4 | Phasing and the cut list |
| Singapore licensing once money or a token arrives | The regulator says it will generally not license token services | No real money in v1; legal advice before any change |

## 8. Decisions still needed from the owner

1. Which first job type for phase 5.
2. Whether the discovery bot's approver is the owner or a hired maintainer.
3. Which desktop technology (affects who can be hired).
4. Whether Japanese and Korean can wait until after the first Taiwan users, as phased here.
