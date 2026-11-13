# Review of status, plan, innovation and goal

Written 2026-11-13, after the first gate-1 sample ran against real providers on the owner's VM. This is a critical review of the whole effort, written to change the plan where the evidence says so. It is my judgement unless a line cites a measurement.

## 1. The verdict in five lines

1. **The infrastructure is real and ahead of plan.** The router, rationing, catalogue, skills, records, permissions, vault, sandbox, home API and a desktop shell exist and pass about 3,100 tests.
2. **Almost none of it is validated.** One 10-run sample with real providers and zero non-technical users is all the outside evidence there is. The sample found a serious defect that 3,000 tests had missed.
3. **The two risks that decide the product are still open.** Whether ordinary people can connect a service has not been tested at all. Whether chores succeed on free allowances is half-answered: the files skill looks good, the research skill failed.
4. **The premise is weaker than a month ago.** Free allowances are shrinking (Oracle halved its allowance, GitHub Models closed, Hugging Face's free plan has no inference credits). "Ration free tiers" cannot be the whole product.
5. **The strongest position is the one the owner started with: infrastructure.** Muse should be the allowance, permission and record layer under any bot, with its own simple app as one of several front ends. Section 5 gives the changes I recommend.

## 2. Where things stand, measured

| Measure | Value |
|---|---|
| Code | About 45,000 lines of Python, 14,000 of Rust, 3,000 of TypeScript, written in three days, almost all by Claude sessions |
| Tests | About 31,000 lines; last full run 3,120 passed, 13 skipped |
| Work packages finished | About 25 of 58 by count |
| Runs against real providers | Two: a key check (9 requests) and the gate-1 sample (74 requests) |
| Non-technical people who have tried anything | 0 |
| Human review of the code | None. CI has not run on the work branch, because it only triggers on pull requests |
| Outside security review of vault, permissions, pairing, sandbox | None |
| Money spent on providers | US$0 |

Against the owner's four-step goal:

| Step | State |
|---|---|
| 1. Infrastructure that rations many kinds of free and cheap services | Strong for AI models. For storage, compute, chain and messaging: listed and dated, not managed |
| 2. A good app, assisted by a bot | A desktop shell with one real screen; four skills, one of them promising on real providers, one failing |
| 3. Take other jobs | Not started. The queue, budgets and output checks it needs exist |
| 4. Data sales, including medical, by decentralised means | Not started. Records and permissions are the foundations; health is parked |

## 3. What the gate-1 sample teaches

Measured on 2026-11-13: 10 runs, 74 requests, 82,306 tokens, nothing spent.

| Skill | Passed its own check | Right by expected facts |
|---|---|---|
| Answer over my files | 5 of 5 | 4 of 5 |
| Research report | 3 of 5 | 0 of 5 |

Four lessons, in order of importance:

1. **Synthetic tests do not find the failures that matter.** The research skill's worst defect, treating an empty model answer as "the files do not say", passed every test because every test used scripted replies. Thirty minutes with real providers found it. Real runs must be routine, not an event.
2. **A skill's own check is only worth what it can prove.** Three wrong reports passed their own check. Word overlap between a claim and a passage proves little. Checking that a claim's quote appears verbatim in the passage, and that its numbers appear in the quote, proves a lot, in any language. That change is in progress and should become the rule for every skill.
3. **Small tasks on reasoning models are a trap.** About a third of the small extraction calls returned nothing, because hidden reasoning used the whole 300-token cap. My earlier floor of 256 tokens was below that cap and would not have helped. The router must know which models reason and treat them differently.
4. **Simple jobs fit free allowances; long chains do not yet.** The files skill used 2 to 3 requests and 2 seconds per run. The research skill used 8 to 18 requests and up to 80 seconds, and was wrong. A first release should lead with short, checkable jobs.

One process failure is mine: the VM's live router was 58 commits behind the skills, and nothing in the harness said so. A run against real providers must print which build answered.

## 4. Honest assessment of the plan

**What is right.**
- Reuse over building: LiteLLM, Biscuit, iroh, x402, the skills format.
- Frozen interfaces with test vectors, so Rust and Python agree by construction.
- Gates with evidence. Gate 1 did its job: it stopped a "done" claim.
- The host adapter. It is the cheapest route to real users.

**What is wrong or risky.**

| Problem | Why it matters |
|---|---|
| Breadth is running ahead of proof | Vault, pairing, permissions and sandbox are built while the two deciding questions are open. If the answers are bad, much of this was built for a product nobody uses |
| Nobody is reviewing | 60,000 lines in three days with no human reader and no CI. Defects compound. The owner cannot review at this rate; the plan assumed five pull requests a week per track and has produced dozens of merges a day |
| Unreviewed cryptography | Four security-critical designs (vault, records, permission tokens, pairing) were written by a model in a day each. They must not hold real secrets until an outside review. Until then they are prototypes |
| The test suite is large and slow | 15 minutes and growing, mostly generated. Its size gives false comfort; see lesson 1 |
| The owner's track has not started | Testers, certificates, counsel, a licence. These have the longest lead times and gate the release more than any code |
| The plan treats "free" as stable | Three free allowances changed or vanished during this project. The catalogue handles change; the product's promise does not |
| Six stale pull requests | #1 to #4 are superseded by the work branch and still open. They confuse anyone arriving |

**What "25 of 58 packages" hides.** The finished packages are the ones a model can build alone in a sandbox. What remains is what it cannot: real devices, signing, app stores, a real cloud account, real users, legal sign-off, a security review. The calendar risk is in the second group, and it has not moved.

## 5. What I recommend changing

### 5.1 Stop widening; prove the core (next two weeks)

Freeze new modules. Finish what is in flight, then put all effort on evidence:

1. **Real runs every night.** Claude Code on the VM, the live router kept current, the gate-1 sample nightly with failures kept. Target: the files skill and recurring chores at 80% right by expected facts, not merely by their own check.
2. **The first five testers this week, not twenty next month.** The mock-up plus the real one-tap sign-in is enough. Five people will show the biggest problems; twenty can follow.
3. **One reviewed, merged trunk.** Close #1 to #4 as superseded, merge #5, open one pull request from the work branch to `main` so CI and the automated reviewer run, and keep it green.
4. **Book the security review** for the vault, permission tokens, pairing and sandbox. Until it reports, permissions stay in "advise" mode and the vault holds nothing real.

### 5.2 Lead with a smaller first release

| In the first release | Held back |
|---|---|
| Router with rationing, hard daily limit, privacy badges | Research reports, until they pass on real providers |
| Answer over my files; recurring chores from files; set up my services | Phones, pairing, the always-on helper |
| Desktop app: Today, Ask, Services, Chores | Wallet, credits, anything on a chain |
| Host adapter for people who already run a bot | Third-party skills |
| Records of everything done | Health |

This is about ten of the remaining packages, not thirty-three.

### 5.3 Two audiences, one layer

The owner chose non-technical people, and that stays the destination. The fastest evidence, though, comes from people who already run a bot: they can install today, they tolerate a key, and they will say what breaks. So:

- **Now:** Muse under existing bots (OpenMausBot, Claude Code) for technical early users, through the host adapter that is already merged.
- **In parallel:** the simple app for non-technical people, tested five at a time.

Both use the same router, permissions and records. Nothing is built twice.

### 5.4 Change the promise

From "use free allowances" to: **"a helper that works within a limit you set, starting at zero."** Free allowances are how most work costs nothing today; a local model and a small paid limit are first-class when free runs out, not a last resort. The app already shows NT$0 of NT$30; the pitch should match it. This survives the next provider cutting its free plan.

## 6. Innovation: what is actually new here

I see four ideas worth building on. The first two are nearly free given what exists.

**1. One allowance engine for every metered thing.** The owner said early that resources are not only language models. The pacing, reserve and ledger code does not care what it counts. LINE's 200 pushed messages a month, Oracle's processor-hours, a storage bucket's operations, test-network settlements: each is a limit, a window and a reset. Make "allowance" a general type that any skill can declare it consumes, and Muse rations all of them with the code it already has. No product found does this, and it is the plainest expression of "infrastructure".

**2. Checked by construction.** Every skill's output is verified by code against its sources (exact quotes, numbers present, files really written) before the person sees it, and the result of that check is written into the record. Small free models become usable because the check, not the model, carries the trust. This also turns each record into a statement of verified work, which is what steps 3 and 4 need.

**3. The mandate compiler.** One approved sentence compiled into a skill token, a router budget, a spending limit, a consent record, and later a health consent. In progress. It remains the centre of the design and the part others lack.

**4. Measured quality as the first data product.** Every opted-in run yields a fact nobody publishes: how often a given free model gets a checked task right, in Traditional Chinese, on a given day. The evaluation set and the chores harness already produce it. A public, dated table of "what free models can really do" has no personal data in it, needs no chain, and is useful to exactly the buyers the owner named (model makers and researchers). It is the lowest-risk way to begin step 4, years before medical data.

How these connect to the goal:

```
Step 1  infrastructure   →  allowance engine for every metered resource   (idea 1)
Step 2  app and bot      →  checked skills under a permission             (ideas 2, 3)
Step 3  other jobs       →  spare allowance spent on jobs whose results
                            code can check; credits from matching records
Step 4  data             →  measured quality first (idea 4); personal and
                            medical data later, under the same permissions
                            and records, after legal and security review
```

Step 3 follows from idea 2: a job can be taken from someone else only if its result can be checked without trusting the worker. Quote-verified extraction, the evaluation tasks, and formal proofs (the earlier design's idea) all qualify.

## 7. Risks, ranked

| Risk | Likelihood | What would show it | Response |
|---|---|---|---|
| Ordinary people will not complete sign-up | High, untested | Five-tester study | One-tap services first; a local model as the no-sign-up path; revisit a small starter allowance |
| Free allowances keep shrinking | High, observed three times | The weekly catalogue check | The changed promise in 5.4 |
| Small models are not good enough for useful chores | Medium, half-tested | Nightly real runs | Short checked jobs; hand-off for the rest |
| A flaw in unreviewed security code | Medium | Outside review | Advise mode, nothing real stored, until reviewed |
| The owner cannot keep up with review | High, already true | Merges per day against reviews per day | Freeze, one trunk, CI, smaller release |
| Licensing of anything touching tokens | Medium | Counsel | Stay on the test network; hold chain work |
| A dependency is compromised again | Medium | The verification script | Pins by hash and digest, already in place |

## 8. Decisions I am asking the owner for

1. **Freeze and prove** (5.1), or continue building to the full plan in parallel.
2. **The smaller first release** (5.2).
3. **Technical early users through host bots now**, alongside the app for non-technical people (5.3).
4. **The changed promise** (5.4).
5. **Make the allowance engine general** (idea 1) the next infrastructure package.
6. **Publish measured model quality** (idea 4) as the first data product, once a licence is chosen and a native reader has checked the test set.
7. From earlier, still open: a licence for the repository; the two vault decisions; the VM's size and cost.

## 9. What I would do in the next seven days if all of the above is accepted

| Day | Work |
|---|---|
| 1 | Merge the research-skill fixes; update the VM's router; re-run the sample with failures kept. One pull request to `main` for CI. Close the stale pull requests |
| 2 | Finish the three packages in flight (permission enforcement in advise mode, app screens, pairing as a prototype). Freeze |
| 3 | Nightly real runs on the VM. Quote verification as the rule in every skill |
| 4 | The general allowance type; LINE pushes and the VM's own processor-hours as the first two non-model allowances actually rationed |
| 5 | Desktop alpha with four screens against the real home; the host-bot guide run for real on the owner's machine |
| 6 | Five testers |
| 7 | Review of what the testers and a week of nightly runs showed; decide gate 1 |
