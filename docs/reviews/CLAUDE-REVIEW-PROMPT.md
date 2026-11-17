# Claude review prompt (design plan v0.2)

Run on your own machine: `gh repo clone zandis/Muse-stack && cd Muse-stack && git checkout docs/design-plan-v0.2`, open Claude Code there, and paste the prompt below.

```
You are an independent senior reviewer. Review READ-ONLY: do not edit files, commit, push, or open issues/PRs.

Context: Muse Stack + Ceresus. Part A "Muse Works" = a LiteLLM routing gateway, useful work on leftover LLM quota verified with Lean 4, results settled via x402 (USDC, Base Sepolia testnet only, no mainnet, no own token), plus priced data/human judgment. Part B = patient-owned health-data stewardship (Taiwan non-profit steward + Ceresus, a Singapore company, as commercial agent), fiat-first, IRB-led, tiers T1 query / T2 train-in-enclave / T3 supervised session / T4 recontact. The audience is the founder; the goal is a global, innovative SaaS that still holds up if frontier labs ship something similar.

Read, in this order:
1. docs/plan/design-plan-v0.2.md (main document, zh-TW; the .pdf has the figures)
2. docs/reviews/external-review-2026-11-12.md and docs/reviews/internal-review-pr2-2026-11-12.md
3. The phase-0 code on branch feat/phase-0-four-layers (PR #2), via `git show`/`git diff main...origin/feat/phase-0-four-layers`, without checking it out over this branch.

Assess:
A. Architecture: is each layer (gateway, circuit breaker, x402 paywall, Lean runner, DataVaultRegistry, receipts/transparency log, unlinkable payout pool) sound and necessary? What is over-engineered, missing, or a single point of failure?
B. Security: verify whether §19.5 (15 must-fix items for PR #2) is complete and correct against the actual code. Lean proof substitution and axioms, sandboxing, replay, auth, EIP-712, on-chain personal data. Name anything we missed.
C. Regulation: Taiwan (虛擬資產服務法 2026, 個資法, 人體研究法 §15, 健保資料管理條例), Singapore (MAS DTSP/PS Act, PDPA), GDPR/EDPB blockchain guidance, HIPAA if US buyers. Flag where the plan is wrong, vague, or overconfident. Mark legal points as needing counsel.
D. Strategy and innovation: are the innovative directions (verification-signal leaderboard, living cohort with recontact, receipts layer, payout pool) defensible against frontier labs and incumbents? Is the Day-45/Day-90 kill-check and mainnet gates G1–G5 realistic?
E. Chapter 21 disagreements: steelman the external review where v0.2 rejected it (clinician-judgment wedge, splitting into two docs, delaying Part B) and say who is right.
F. Internal consistency: contradictions between chapters, numbers, or between plan and code.

Output (in Traditional Chinese, with English technical terms kept):
1. A 5-line verdict.
2. Findings table: ID | severity (Critical/High/Medium/Low) | area (A–F) | section or file:line | issue | concrete fix.
3. Top 5 changes for v0.3, ranked.
4. Open questions only the founder can answer.
Separate facts you verified in the files from your judgment, and don't invent figures or citations; say "unverified" when unsure.
```
