# Wardmuse: detailed plan

2026-11-12. Drafted from the owner's answers (recorded in `docs/plan-infra-first.md`) and two research passes done today: a competitive check and a free-tier inventory. This document adds the reasoning: where Wardmuse is competitive, how rationing creates the most value, and what the user sees.

**Evidence.** Research figures are mostly from secondary sources; where a provider's own page was read, the text says so. Prices and free limits change monthly. Legal points are design judgment, not advice. Section 12 lists what was not verified.

---

## 1. The product in one page

**Wardmuse is a helper that does your chores using the AI services you already qualify for, and never surprises your wallet.**

Under it is one engine: a router that knows every allowance the user has, when each refills, and what each is good for, and spends them in the order that gets the most done for nothing.

| Layer | What the user gets |
|---|---|
| **Helper (the bot)** | Answers over their own files, research reports, recurring chores, and setting up their services for them |
| **Allowance engine (the router)** | Everything above runs on free allowance first; paid services only inside a daily cap the user sets |
| **One endpoint** | Other apps and automations can use the same rationed allowance |
| **Trust layer** | Data encrypted with a key only the user holds; every action under a permission they granted; a receipt for each |
| **Later** | Exchange of spare local compute for decaying credits; consented data sales; medical as a separate add-on |

Fixed decisions: any individual, non-technical; desktop and mobile on one Rust core with Tauri apps; the user's own accounts with guided sign-up; free for users; open core; Traditional Chinese and English first; Singapore company, Taiwan users first.

---

## 2. What the research changed

### 2.1 "Saves money on chat" is not the pitch

- A heavy personal chat user costs under about US$3 a month at late-2026 cheap API prices. The inventory puts a realistic month (100 chat turns a day plus eight long jobs) at about US$1.6 on a small model and about US$31 on a strong one.
- A Taiwan Fair Trade Commission survey released 29 October 2026 found 87.9% of generative-AI users use ChatGPT and 67.8% use Gemini. Of those who do not pay, 89.5% say the free allowance is enough.
- Chunghwa Telecom and Taiwan Mobile bundle paid AI tiers with phone plans.

For plain chat, free ChatGPT plus free Gemini already serve most people. Wardmuse must not compete there.

### 2.2 Where the value is

Chores, research and file indexing use roughly ten times the tokens of chat. That is where allowances matter and where today's products charge:

| Product | Price | Main complaint found |
|---|---|---|
| Manus | US$20–200 a month in credits | One task can burn 500–900 credits with no preview |
| Genspark | about US$25 a month | Opaque credits |
| Lindy | from about US$30–50 a month | Expensive |
| OpenClaw (self-hosted) | free | Heavy public security record; cut off from Claude subscriptions in 2026 |

So the claim Wardmuse can defend is: **the same kind of helper, running on allowances you already have, with a hard ceiling on spending and a record of everything it did.**

### 2.3 The gap nobody fills

No product was found whose core promise is rationing free allowances across apps for ordinary people. The closest are a self-hosted developer tool that pools free providers, and a Mac menu-bar tool that only displays coding-tool quotas.

### 2.4 The honest size of the free allowance

From the inventory, with low confidence on the numbers:

- Theoretical value of every free AI allowance fully used: about US$40–60 a month. Nobody can use them that way.
- Realistic value captured by one person: about US$2–5 a month against small-model prices.
- It becomes large (over US$20 a month) only for token-heavy work that tolerates smaller models: batch chores, research, indexing, coding agents.

That is consistent with 2.2: the helper is the heavy user, and rationing is what lets it run free.

---

## 3. Positioning

Three statements tested against the evidence, strongest first:

1. **"Works with the ChatGPT and Gemini you already use."** Fits existing habits (87.9% and 67.8%). Wardmuse hands hard questions to the user's own app and takes care of everything those apps do not do.
2. **"A helper that never surprises your wallet."** Answers the main complaint about paid agents. Speaks to the 39.8% of Taiwan AI users who have paid for something.
3. **"Private, with a receipt for everything."** Credible against the security record of open agents. No evidence found that consumers pay for privacy, so this builds trust; it does not drive revenue.

What Wardmuse does not say: "unlimited free AI", "bypass limits", "cheaper ChatGPT". The first two are also legally unwise (see `docs/plan-infra-first.md` §8).

---

## 4. Competitive check

### 4.1 Closest alternatives

| Alternative | For whom | Price | It does, Wardmuse does not | Wardmuse does, it does not |
|---|---|---|---|---|
| Free ChatGPT / Gemini apps | Everyone | Free; about NT$270–290 for ChatGPT Go | Best models, voice, images, brand | Chores on a schedule, answers across your files, a spending cap, an endpoint for other apps, receipts |
| Carrier bundles (Chunghwa + Google AI, Taiwan Mobile + Perplexity) | Their subscribers | Free for a period | A frontier paid tier at no cost | Works across providers; automation |
| Manus, Genspark, Lindy | People who want an agent | US$20–200 a month | Stronger models; runs in their cloud | Predictable cost; local encryption; uses your free allowance |
| OpenClaw and forks | Technical early adopters | Free, self-hosted | Many connectors; large community | Safe defaults; no setup; permissions and receipts |
| OpenRouter | Developers | 5.5% on credit purchases | Hundreds of models | Plain-language setup; quota view; phone app; chores |
| Poe, t3.chat | Hobbyists | US$5–20 a month | Many premium models for a flat fee | Free allowance; cap; local data |
| Duck.ai, Proton Lumo | Privacy-minded users | Free to about US$13 a month | Established privacy brands | Acts on your files and accounts |
| LINE and Kakao built-in AI | Their users | Free so far | Already in the app people open daily | Cross-provider; your own data control |
| AI Horde | Hobbyists sharing GPUs | Free; credits earned by serving | A working compute exchange today | Credits that decay; a general helper |

### 4.2 Where Wardmuse wins

- The only product found that leads with rationing and a hard spending cap for non-developers.
- A helper whose running cost is near zero for the user, against agents priced by opaque credits.
- Permissions and receipts by design.
- Honest hand-off to the user's own subscription app.
- One endpoint ordinary people can give to other apps.

### 4.3 Where Wardmuse loses

- Plain chat: the free apps are better and already installed.
- Model strength: free allowances are mostly smaller models. Chores must be designed to succeed on them, with hand-off for the rest.
- Setup: connecting services is a step the free apps do not have.
- Stability: free allowances move without notice. GitHub Models was retired on 30 July 2026; Gemini's free limits fell during 2026.
- Reach: LINE, Siri and Android assistants are one tap away.

### 4.4 With a fee

The core stays free, as decided. A paid tier is plausible at about **US$3–5 a month** (roughly NT$99–149):

| | Evidence |
|---|---|
| Price fits | Below ChatGPT Go in Taiwan; at Poe's entry price; an ordinary app-store amount |
| What it must contain | Things that cost the company something or save the user real effort: sync across devices, the always-on worker set up for them, more scheduled chores, premium skills |
| Expected take-up | Low. Subscription-app benchmarks put AI apps' annual retention near 21%. Plan on a low single-digit share of active users paying. |
| Worst-case total cost to a user | Paid tier plus the daily cap fully used: about US$35 a month. Typical: the tier plus well under US$3 of capped spending. |

Against a US$20–40 agent subscription, a helper at US$3–5 plus a visible cap is competitive **if its chores succeed on smaller models**. That condition is the main product risk and is tested in phase 0 (section 10).

Revenue from a cut on earnings has no base yet. It should not appear in any forecast before steps 3 and 4 have users.

---

## 5. Rationing for the greatest value

### 5.1 The idea

Every allowance is perishable in its own way. Value is lost in three ways: a strong allowance is spent on a small task; an allowance resets unused; money is spent when something free would have done. The router's job is to avoid all three while never making the user wait.

### 5.2 Kinds of allowance

| Kind | Examples (from the inventory) | How the router treats it |
|---|---|---|
| **Perishable, daily** | Gemini requests (reset midnight Pacific), OpenRouter free models (UTC day), Cloudflare Workers AI (UTC day), Groq, Hugging Face ZeroGPU (24 hours from first use) | Spend first. Release to background work as the reset approaches. |
| **Slow-renewing, monthly** | Cohere trial calls, x402 free settlements, LINE message quota | Pace evenly across the month. |
| **One-off** | Trial credits that never renew | Hold. Use only when nothing else can do the job, with the user's approval. |
| **Scarce and strong** | The best free model with a small daily cap | Reserve for tasks that need it. |
| **Abundant and small** | Small fast models with large daily caps | Default for everyday work. |
| **Local** | A model on the user's own device | No cap, always private, slower and weaker. First choice for trivial and private tasks. |
| **Paid** | Low-cost APIs | Last, inside the daily cap, only when the price is known. |
| **Subscription** | The user's own ChatGPT, Claude or Gemini app | Never routed through. Offered as a hand-off for the hardest questions. |

### 5.3 Kinds of task

| Tier | Examples | Needs |
|---|---|---|
| **Light** | Classify, extract, short rewrite, tag a file | Any model, including local |
| **Everyday** | Chat, summaries, drafting, translation | A solid small model |
| **Heavy** | Long documents, multi-step research, synthesis | A strong or large-context model |
| **Frontier** | Questions where being wrong is costly | Hand-off to the user's subscription app, or a paid strong model with approval |

The helper labels its own steps. Most of a research job is light and everyday work (searching, reading, extracting); only the final synthesis is heavy.

### 5.4 The rules, in order

These are the readable fixed rules the owner chose. Rules 1, 3 and 5 are built and tested in the reference router today; the rest are specified here.

1. **Privacy first.** A request only goes where its privacy class allows. Nothing below overrides this.
2. **Fit the task.** Use the weakest allowance that can do the task well. Strong calls are not spent on light work.
3. **Protect what the user is doing.** The app in use gets what it asks for. Background work keeps its hands off a reserve of each allowance and yields when the user is active.
4. **Spend what expires soonest.** Among allowances that fit, prefer the one that will reset with the most left over.
5. **Money last, and capped.** Paid services only when nothing free fits, only inside the daily cap, only when the price is known. The helper shows an estimate before a large job.
6. **Offer the hand-off.** For a frontier task, propose opening the user's own subscription app before proposing to spend.
7. **Hold one-offs.** Trial credits wait for a job that needs them.

### 5.5 Pacing: how rule 4 works

Borrowed from how advertising budgets are paced. For each allowance the router draws a straight line from full at the last reset to empty at the next. Actual use is compared to that line.

- **Under the line** (use is behind schedule): the allowance is in surplus. Background chores may take it.
- **On or over the line:** the allowance is being used fast enough. Background work waits.
- **Close to reset with allowance left:** the reserve is lifted, and queued chores soak up what would have been wasted.

Where a provider reports nothing (Gemini publishes no remaining count), the router counts its own calls against the limit in the catalogue and marks the figure as an estimate.

### 5.6 Spending less in the first place

Cheaper than choosing well is not asking at all:

- **Exact repeats** are answered from a local cache.
- **Only the relevant passages** of a file are sent, never the whole file.
- **Light steps run on the local model** when the device has one.
- **Chores are scheduled just before a reset**, so they use what is left.
- **Long prompts keep a fixed opening**, which providers bill at a discount.

### 5.7 A worked day

A user with four connected services and no local model:

| Time | What happens | Allowance used |
|---|---|---|
| 08:00 | Morning digest chore (scheduled the night before to finish before Gemini's reset) | The previous day's leftover small-model allowance |
| 10:15 | User asks about a contract in their files | Everyday tier; small model; three passages sent |
| 13:00 | User starts a research report | Reading and extraction on abundant small models; final synthesis on the one strong free model |
| 13:20 | Strong model's daily allowance is used up | Helper offers: wait until reset, hand off to the user's ChatGPT app, or spend about US$0.04 |
| 16:00 | User is idle; file indexing resumes | Surplus under the pacing line |
| 23:30 | Reset approaching, allowance left | Reserve lifted; queued chores run |

Spent: nothing, or four cents if the user chose that.

### 5.8 What the user sees of this

One sentence under each answer and each chore, on request: *"Used your Groq allowance (resets in 3 h). Nothing spent."* And one number on the home screen (section 6).

Value is reported honestly: work done, share run on free allowance, and money spent against the cap. Not an inflated "you saved" figure, because against small-model prices the saving on chat is cents.

---

## 6. User experience

### 6.1 Principles

1. **One number.** The home screen answers "can my helper work right now, and will it cost me anything?"
2. **Plain words.** Helpers, allowance, refills. Model names and tokens stay behind a "details" tap.
3. **Value in the first minute.** A first answer before any key is pasted.
4. **One step at a time.** The helper proposes the next connection when it would help, with what the user gains and how long it takes.
5. **Nothing happens behind the user's back.** Every write is a card to confirm. Every action is in the activity list. One switch pauses everything.
6. **The same on phone and desktop.** Phone first.

### 6.2 The first minute

Guided key sign-up is the riskiest step in the product: no published data shows non-technical users complete it. So the first connection uses no key at all.

| Step | Screen | Taps |
|---|---|---|
| 1 | Language, one sentence on what Wardmuse is, one on what it is not | 1 |
| 2 | "Connect your first helper": one button that opens OpenRouter's own approval page and returns with a key the app receives directly | 2 |
| 3 | "Ask something, or pick a first chore" with three suggestions | 1 |

OpenRouter's one-tap connection was confirmed on its own documentation today. Its free allowance is small (50 requests a day until the user has bought US$10 of credit) and some of its free models train on inputs, so the helper says both plainly and proposes a second helper within the first session.

On a device that can run a small local model, that is offered as an alternative first helper: nothing to sign up for, fully private.

### 6.3 Adding more helpers

The helper suggests the next service when it would have helped: *"That would have gone faster with Gemini connected. About 2 minutes, 3 steps. Adds roughly N requests a day."*

For services with no one-tap connection:

1. Wardmuse opens the provider's own key page.
2. A card over the page shows the current step, one at a time, in the user's language.
3. When the user copies the key, Wardmuse notices and asks "Use this key?"
4. Wardmuse tests it and shows what was gained.

Wardmuse guides; it never operates the provider's website for the user. Steps live in the catalogue as data, so the discovery bot can flag a guide when a provider changes its page.

Order of suggestions: most allowance gained per minute of setup. From the inventory (judgment, not measured): OpenRouter, Gemini, Groq, Cloudflare Workers AI, Mistral.

### 6.4 Screens

| Screen | What is on it |
|---|---|
| **Today** (home) | One gauge: how much of today's free allowance is left, and when the next refill is. Below: chores running or done, anything waiting for the user's decision, money spent today against the cap. |
| **Ask** | Conversation with the helper. Under each answer, one line: which helper answered and what it cost. A button to open the same question in the user's ChatGPT, Claude or Gemini app. Confirm cards for anything the helper wants to change. |
| **Chores** | A list in plain language ("Every Monday, summarise these feeds"). Each shows its budget, its last result, and when it runs next. Adding one is a sentence. |
| **Helpers** | One card per connected service: what is left, when it refills, what it is good for, a privacy badge. Storage, compute and chain allowances appear here too, shown and not yet automated. "Add a helper", ranked. |
| **Activity** | Every action as a readable line, newest first. Filters: "anything that left my device", "anything that cost money". Undo for reversible actions. The pause switch. |
| **Settings** | Daily cap, default privacy, permissions granted, memory (readable and editable), languages, export. |

Desktop adds a menu-bar icon with the gauge and next refill, and a page that gives other apps Wardmuse's endpoint with a key per app.

### 6.5 Hand-off to a subscription

1. The helper recognises a frontier question, or the user taps "Open in my ChatGPT".
2. Wardmuse prepares the question with the relevant context and opens the provider's own app with it.
3. The user runs it there, under their own subscription.
4. Sharing the answer back to Wardmuse (the phone's share sheet, or copy) files it with the conversation.

Nothing is routed through the subscription. This is always within the providers' terms.

### 6.6 Simple-step targets

| Flow | Target |
|---|---|
| Install to first answer | Under 1 minute, 4 taps, no key |
| Adding a service with one-tap connection | Under 1 minute |
| Adding a service by guided key | Under 3 minutes |
| Creating a chore | One sentence and one confirmation |
| Finding out what the helper did today | One tap from home |
| Stopping everything | One tap from any screen |

---

## 7. The helper

Operating rules are fixed in `docs/plan-infra-first.md` §6. In summary: one helper that loads skills; runs on the user's devices, with an optional worker in the user's own free cloud allowance; started by the user, a schedule, an event, or expiring allowance; a default budget per job type shown before it starts; reads freely and confirms every write; acts as the user after confirmation; when unsure, finishes what is safe and asks one question; deals with other helpers only inside a standing permission; daily summary, pause switch, undo list, alerts.

### 7.1 First skills

| Skill | Why first | Designed for small models by |
|---|---|---|
| **Set up my services** | Moves the 90-day number directly | Mostly rules and guides; little model use |
| **Answer over my files** | The free apps do it poorly | Retrieval does the work; the model only reads a few passages |
| **Research report** | Token-heavy, so allowances matter | Many light steps, one heavy step, hand-off offered for the synthesis |
| **Recurring chores** | Uses expiring allowance; gives a reason to return daily | Fixed templates with a plain-language front end |

### 7.2 Quality on small models

The whole case rests on chores succeeding on free allowances. Three safeguards:

- **Check before delivering.** Each skill has a cheap test of its own output (does every claim cite a passage; does the summary cover each source). A failed check triggers one retry on a stronger allowance, then a hand-off offer.
- **Say what it could not do.** A partial result with a clear note beats a confident wrong one.
- **Measure it.** Per skill, the share of runs that pass their check on each allowance. This is also the first usage statistic users could later choose to sell.

---

## 8. Credits and chain

Fixed in `docs/plan-infra-first.md` §7 and §10. In summary:

- Credits are created only when one user's own resource serves another and both sign the receipt. They halve every 30 days. A new user has a small overdraft that grows with history.
- First job type: spare local compute.
- The helper's permissions are installed as smart-account limits, so the chain refuses anything outside them.
- Receipts and consents stay off-chain; only batch roots are anchored. Base, test network only, no real money in v1.
- Later: a user co-operative, one member one vote, surplus shared by contribution.

Two findings from today's research to design around:

- **AI Horde is the closest precedent.** Its credits are earned by serving, cannot be bought or sold, and have run for years. They do not decay. Its experience with abuse and queue fairness should be studied before building.
- **Peer-to-peer inference on home machines is slow and unreliable.** The exchange should serve background chores, which tolerate delay, and should not sit on the path of anything the user is waiting for.

---

## 9. Innovations in this plan

| # | Idea | Status |
|---|---|---|
| 1 | Pacing free allowances the way advertising budgets are paced, with reserves for the active app | Reserve and yield rules built in the reference router; pacing line specified |
| 2 | Chores scheduled onto allowance that is about to expire | Specified; no published method was found |
| 3 | A first helper connected in one tap with no key, then services added one at a time when they would have helped | Specified; OpenRouter flow confirmed |
| 4 | The helper sets up the user's services as its first job | Specified |
| 5 | Hand-off to the user's own subscription app instead of routing through it | Specified |
| 6 | One object for a permission, a data consent and an on-chain spending limit | Permissions and receipts built; on-chain form specified |
| 7 | Privacy classes that refuse instead of falling back | Built and tested against a real proxy |
| 8 | Credits that decay like the capacity behind them, created only by two-sided receipts | Specified |
| 9 | A catalogue of free allowances across AI, storage, compute and chains, kept current by a bot with human approval | AI catalogue built; schema for the other three drafted |
| 10 | Each skill checks its own output and reports its pass rate per allowance | Specified |

---

## 10. Build plan

### Phase 0: two tests before building the apps (weeks 1–2)

These two answers decide whether the rest is worth building.

| Test | How | Pass | If it fails |
|---|---|---|---|
| **Will non-technical people connect?** | Twenty Taiwan testers; a clickable prototype with the real OpenRouter one-tap flow and one guided-key flow | At least 15 of 20 get a first answer within 3 minutes; at least 10 add a second service unaided | Rework setup before anything else. Fallback to consider: a small company-funded starter allowance, which the owner rejected once and would need to revisit. |
| **Do chores succeed on free allowances?** | Run the four first skills on real free tiers, 50 runs each, with the owner's keys, using the reference router | At least 80% pass their own check with nothing spent | Narrow the skills, or lead with hand-off and the cap |

The second test also exercises the router with real providers for the first time.

### Phases 1–6 (weeks 1–12)

As in `docs/plan-infra-first.md` §4: router that rations; portable Rust core; guided setup and apps; catalogue beyond AI; general helper; credits and exchange on a test network. Cut order if time runs short: chat-app interface, email and calendar, always-on worker, second job type. Phones ship together or after week twelve.

### Numbers to watch

| Number | Why |
|---|---|
| Time from install to first answer | The first-minute promise |
| Services connected per user at day 7 | The owner's 90-day number |
| Chores active per user | Whether the helper is used beyond chat |
| Share of work done on free allowance | Whether rationing works |
| Paid spend per user per day | Whether the cap is felt |
| Skill pass rate per allowance | Whether small models are enough |
| Weekly active users; return at day 30 | The seed proof |

---

## 11. Risks

| Risk | Evidence | What reduces it |
|---|---|---|
| Free apps are enough | 89.5% of non-paying Taiwan users say so | Lead with chores and files, not chat |
| Setup drop-off | No data that key pasting works for ordinary people | One-tap first; phase 0 test |
| Chores fail on small models | Untested | Phase 0 test; self-checks; hand-off |
| Allowances shrink or vanish | GitHub Models retired; Gemini free limits cut in 2026 | Many providers; dated catalogue; discovery bot; cheap paid tier under the cap |
| Providers act against aggregation | Anthropic and Google acted against subscription logins; no action found against one person using their own free keys | Own accounts only; one per provider; honour every limit; hand-off for subscriptions; a terms read per provider |
| A helper mistake or prompt injection | OpenClaw's record | Confirm every write; permissions; receipts; few connectors at launch |
| Assistants built into the phone and chat apps | Siri with Gemini announced; LINE and Kakao assistants live | Be the neutral layer across them; do what they will not |
| Scope | Rust rebuild plus two phone platforms in twelve weeks | Phasing and cut list |
| Google account connectors need a paid security assessment | Reported for broad Drive and Gmail access; cost estimates conflict | Start with the narrowest file permission; confirm before building |
| No buyer for jobs or data | None identified | One job type; no forecast revenue from it |

---

## 12. Not verified

- Current free limits for most providers. Only OpenRouter, Cloudflare Workers AI, Hugging Face ZeroGPU and Gemini's reset rule were read on the providers' own pages today.
- Whether each provider's terms allow one person to use their free key through a tool like this. Read for OpenRouter, Cohere and Groq earlier; not for the rest.
- One-tap connection for any provider other than OpenRouter.
- Availability of each service in Taiwan, Japan and Korea.
- Storage, compute and chain allowances, apart from the x402 free settlements and the ZeroGPU quota.
- Whether anything can run always-on for free. Oracle's free allocation is disputed; Render sleeps; Fly.io appears to have ended free use for new accounts.
- The cheap-model prices used for the cost estimates.
- Any data on non-technical users completing a key-paste flow.
- Whether consumers pay for privacy.
- AI Horde's current scale and abuse record.
- The paid-tier take-up rate; the figure used is an assumption.
- Whether decaying service-issued credits and a contribution-based co-operative stay outside payment and securities rules.
