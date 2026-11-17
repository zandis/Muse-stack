# Independent review: zandis/Muse-stack PR #2

**Reviewer posture:** skeptical senior reviewer who did not write this PR.
**Scope:** `feat/phase-0-four-layers` (`348db21`) vs base `cursor/muse-stack-gateway-d29f` (`203d0b6`).
**PR:** https://github.com/zandis/Muse-stack/pull/2 — stacked on #1, explicitly “do not merge unless asked.”
**Local checkout:** `/workspace/muse-phase0/Muse-stack` (clean worktree, read-only; no push, no PR comment, no approval, no repo edits).
**Design doc:** `/workspace/docs/Muse-Stack-Ceresus-Design-Plan.docx` (v0.1, 2026-11-12).
**Date:** 2026-11-12, Asia/Taipei.

---

## Verdict

**Not merge-ready**, even as a phase-0 skeleton, until the three items in “What must change first” are fixed. After those, this is a reasonable stacked draft to keep open and iterate on — it should still not be merged to a default branch, which the PR already says.

The 44 pytest + 15 Foundry tests are all green (locally and on GitHub Actions). That is not evidence the trust mechanisms work. The two things this product’s design says a machine can decide — “the Lean proof is of this statement” and “this result was paid for” — are both unsound in the code that would be extended into later phases. The tests never exercise those invariants, so they give false confidence.

This is a competent sketch with good hygiene (mock-only facilitator, hardcoded `MockVerifier`, `settled: false`, secrets not echoed, no deploy script, EIP-712 domain bound to `chainId` + `verifyingContract`, high-s rejected). It is not yet a foundation you can build the Ceresus market on.

---

## Method

Read the full PR diff (53 files, +4332/−1), `docs/ARCHITECTURE.md`, `docs/LEAN.md`, and the design plan (routing 4.5, settlement 5, Lean 6.4–6.6, medical contracts 8.4/8.11, STRIDE 9.2, backlog 13.4). Ran:

| Suite | Result |
| --- | --- |
| `pytest tests -v` (venv, Python 3.13) | **44 passed** in 0.73s |
| `forge test -vv --fuzz-runs 256` (Foundry 1.9.0, forge-std already in gitignored `contracts/lib`) | **15 passed**, 0 failed, including `testFuzz_onlyOwnerCanRevoke` (256 runs) |
| Official GitHub Actions on this branch (`43167854519`, `43167844624`) | `validate`, `boot`, `phase0-python`, `phase0-contracts` all **pass**. CodeRabbit skipped (non-default base). |
| Extra Python probes in `/tmp/muse-review/probes.py` (not in the repo) | Confirmed statement-swap, payment replay, resource unbinding, job-API proof leak, JobRunner ignoring open circuits, startup mainnet-guard case hole, stuck half-open probe |
| Extra Foundry probes in `/tmp/muse-review/sol-probe` (imports repo sources, does not modify them) | 10/10 passed: vault-id squat, self-attested hospital, signature with no deadline, `accessExpiresAt` ignored off `TimedAccess`, `setTier` escaping a timed expiry, revoke leaving storage, wrong-subject signature correctly rejected |

Did not run a real `lean` binary (none on PATH). Statement-swap and axiom gaps in real mode are shown by assembling the source the verifier would feed Lean, which does not require the binary.

### External review cited by the parent

The steering note asked me to confirm or refute an external review at

`/home/box/agent-data/agents/33a5bb5a-9c21-443f-bac8-a63f52385aef/attachments/00244a478250a6b979309f8f5cc0182c00e4a6fc94ff9657111e71fc8a856244.md`

(section 7 allegedly lists 14 PR #2 findings), and to list only *new* findings it missed. **That file is not on disk.** The attachments directory has the design `.docx`, deck images, and a `.pptx`, but no markdown with that hash. There is no review file in the repo, no GitHub review on PR #2 (CodeRabbit skipped), and no matching document under `/workspace` or `/tmp`. I therefore cannot confirm or refute those 14 items by number. This document is a complete independent findings list, not a delta. Foundry tests, which that review reportedly did not run, were run locally and are reported above.

---

## What is solid (so the findings are in context)

- Phase 0 will not start with `MUSE_X402_FACILITATOR` other than `mock`, and `server.py` always constructs `MockVerifier`. Receipts always have `settled: false`, `mode: "mock"`, and a “no funds moved” detail. A live EIP-3009 payload is rejected (`not_mock`). There is no RPC, no broadcast, no deploy script, `ffi = false`, and Foundry `broadcast/` is gitignored.
- The mock verifier *does* lowercase before the mainnet check, so a payment that says `Mainnet` / `EIP155:8453` is refused even when startup’s denylist misses it (see M2).
- `/v1/providers/keys` returns only `configured` / `missing`. `.env.example` has empty key values. `redact` / `assert_no_secrets` are used on health/keys (not on every path).
- Circuit breaker: RFC 9110 delta-seconds and HTTP-date parse correctly for the cases the tests cover; delay is capped at one hour; a missing header uses the default cooldown; a lock plus `probe_in_flight` allows only one concurrent half-open probe (confirmed with 20 threads).
- `DataVaultRegistry`: owner-only `setConsent` / `setTier` / `revoke`; revoke is permanent; timed-access requires a future expiry; EIP-712 digest binds `vaultId`, `dataHash`, `subject`, `hospital`, `chainId`, and `verifyingContract`; malleable high-s signatures are rejected; a hospital signature for subject A cannot be used by subject B (extra probe). No payable functions.
- Official Foundry tests are meaningful on those owner and signature paths, not just compilation smoke tests. Register is expensive (~214k gas avg) because the 65-byte signature is stored; that is a design smell, not a test failure.
- CI adds `phase0-python` and `phase0-contracts` next to unchanged `validate` and `boot` jobs. The PR does not merge #1 and does not force-push existing branches.

---

## Prioritized findings

Severity:

- **Blocker** — a core claimed invariant is false; must fix before anyone treats a “verified” hash or a 402 sale as meaningful, including in `MUSE_LEAN_MODE=real`.
- **High** — wrong behaviour on a path this PR presents as working, or a footgun that a later “swap in the real implementation” will inherit.
- **Medium** — real bug or design gap, not immediately fatal for a laptop stub.
- **Low** — hygiene, test gaps, docs, or defence-in-depth.

---

### Blockers

#### B1. Real Lean will “verify” a different theorem than the one being sold

- **Severity:** Blocker
- **Where:** `muse/useful_work/lean.py:233-243` (`_assemble_source`), used at `:151` by `_real`; hash binding in `muse/useful_work/runner.py:165-174`
- **Issue:** If the model’s proof starts with `theorem` or `lemma`, the original problem statement is discarded and the model’s text is the entire Lean file. A probe assembled this source for `sample.nat_add_comm`:

  ```lean
  theorem totally_different : True := by
    trivial
  ```

  The original `a + b = b + a` is gone. In real mode, Lean exit 0 plus a substring check for `sorry`/`admit` is enough to mark `verdict: "verified"`. The result hash still includes `statement_hash` of the *original* statement (`runner.py:167`), not of the source that was checked. A buyer looking at the hash is told the original theorem was proved. This is exactly the STRIDE row in the design plan (9.2: “修改 Lean 題目陳述以「證明」別的命題”) and the reason section 6.4 gives for a statement-hash lock: “只要允許陳述被偷偷改寫……就能『證明』任何東西。” The hash is computed, then not used as a lock.
- **Fix:** Never let the model replace the statement. Assemble only by substituting the `sorry` of a *normalized* copy of `problem.statement`. Before accepting exit 0, hash the theorem statement that Lean actually checked and require it to equal `statement_hash`. Reject the job if they differ. Add a test that a `theorem True` proof of `sample.nat_add_comm` is rejected even if a fake Lean returns 0 (inject the assembler, not the binary).

#### B2. Real Lean accepts unsound proofs: `axiom`, `native_decide`, `skipKernelTC`, etc.

- **Severity:** Blocker
- **Where:** `muse/useful_work/lean.py:185-193` (`_real`), `:206-211` (`_contains_sorry`); `docs/LEAN.md` (“Exit code 0 and no `sorry` … counts as verified”)
- **Issue:** After Lean returns 0, the only extra check is a raw substring search for `sorry` and `admit`. Probes confirmed `_contains_sorry` does **not** fire on `axiom boom : True`, `by native_decide`, `set_option debug.skipKernelTC true`, `uncheckedCast`, or `implemented_by`. A model that starts its payload with `theorem` (B1) can submit:

  ```lean
  theorem sample_nat_add_comm (a b : Nat) : a + b = b + a := by native_decide
  ```

  or declare an `axiom` and “prove” the goal from it. The design plan (6.4, BL-13) requires `lake env lean --run print_axioms` and an allow-list (`propext`, `Classical.choice`, `Quot.sound`). None of that is here. `_contains_sorry` is also the wrong tool for the cases it *does* try to catch: it false-positives on comments and identifiers (`sorryful`, `sorryAx`) and only strips `--` line comments, not `/- -/` blocks, in the mock path.
- **Fix:** In real mode, (1) pin the statement (B1); (2) dump axioms and reject anything outside a whitelist; (3) reject `native_decide` / `nativeDecide`, `uncheckedCast`, `implemented_by` / `extern`, `unsafe`, and `set_option debug.skipKernelTC`; (4) use a Lean parser or kernel axiom report, not a substring. Until that exists, `MUSE_LEAN_MODE=real` should refuse to emit `verdict: "verified"` (return `error` / “real verifier not sound yet”) so no unsound hash can be listed for sale.

---

### High

#### H1. Untrusted Lean runs unsandboxed with the full process environment

- **Severity:** High
- **Where:** `muse/useful_work/lean.py:156-163`
- **Issue:** `_real` runs `subprocess.run([lean, file], env={**os.environ, ...}, timeout=30)` with no user namespace, no seccomp, no network cut, no filesystem jail (the temp dir is only where the `.lean` file is written), and no memory cap. Lean 4 elaborates `#eval` at compile time and has `IO` (`IO.FS`, `IO.Process`, environment access). Model output can read `GROQ_API_KEY` / `LITELLM_MASTER_KEY` from the inherited env, exfiltrate them, and write outside the temp dir. The design plan (3.3, 6.4, BL-10) requires a no-network container. `docs/LEAN.md` never mentions this.
- **Fix:** Do not turn on real mode until Lean runs in a network-none, read-only, secret-free environment (dropped env, explicit `LEAN_PATH` only, rlimits, optional seccomp/bubblewrap or a container). Pass a scrubbed `env`. This is a prerequisite for B2’s real mode, not a follow-up.

#### H2. The 402 payment wall is bypassed by the job API; payments are not bound to a resource and are infinitely replayable

- **Severity:** High
- **Where:** `muse/server.py:220-251` (job create/get), `muse/settlement/verifier.py:79-133` and `:135-143`, `muse/server.py:268-301`
- **Issue:** Confirmed with `TestClient`:

  1. `POST /v1/work/jobs` returns HTTP 201 with the full `proof` and `listedForSale` set to the *same* id as `jobId`.
  2. `GET /v1/work/jobs/{job_id}` returns that proof again (`JobRecord.as_dict(include_proof=True)` is the default). `GET /v1/results/{id}` is the only path that returns 402. `docs/ARCHITECTURE.md` describes the job GET as “status, verdict, result hash” and does not mention the proof — docs vs code.
  3. The same `PAYMENT-SIGNATURE` was accepted twice on one result and once on `demo-nat-add-comm`: no nonce ledger, `validBefore` / `validAfter` ignored (a payload with `validBefore=1` and `validAfter=9999999999` was accepted), no `resultId` / resource in the verified fields. One mock payment unlocks every result at or below that price.
  4. `scheme` is documented as required to be `exact` and is never checked (`scheme: "upto"` was accepted). Amount is `>=` the quote, not exact.

  For a mock this does not move money. It is the exact API shape a later “swap in a real facilitator” would inherit, and it trains tests that a 200 means nothing about uniqueness or which item was paid for. The design’s settlement layer (5.2–5.3) is a per-resource EIP-3009 authorization with a nonce.
- **Fix:** (a) Job GET/POST must not return `proof` (or only to an admin). Listing for sale should be a separate step. (b) Even the mock verifier must bind `resource` / `resultId`, record `(payer, nonce, resource)` as spent, and honour `validBefore` / `validAfter`. (c) Check `scheme == "exact"` and amount equality if that is the scheme. Add tests for replay, cross-resource use, and expiry — these currently do not exist.

#### H3. The circuit breaker is not on the two paths that actually send traffic

- **Severity:** High (correctness of the headline feature)
- **Where:** `muse/useful_work/runner.py:132-152`; LiteLLM still has `cooldown_time: 30` in `scripts/render_config.py:174`; `docs/ARCHITECTURE.md:121-124` already admits the proxy does not read `Retry-After`
- **Issue:** The new breaker and `FallbackChain` work in isolation (tests are fine). They are not used where it matters:

  1. **Useful-work runner** — the only LLM client in the phase-0 service — loops aliases and, on a 429, calls `router.report_event` (which *opens* the circuit, using the alias name `smart`/`fast` as if it were a provider). It never calls `breaker.allow()` or `GatewayRouter.execute()`. A probe ran two jobs: after job 1, `smart` was `open`; job 2 still called `smart` first and got another 429. The breaker is a dashboard, not a breaker, on this path. `ARCHITECTURE.md` says “A 429 from the proxy does [open a provider circuit]” but does not say the next job skips it — because it doesn’t.
  2. **The LiteLLM proxy on :4000**, which is the actual user-facing gateway, is unchanged from PR #1: fixed 30-second cooldown, no `Retry-After`. The design plan marks this as BL-03 (P0): “Retry-After 斷路器（LiteLLM callback）；Redis 共享狀態,” with exit criterion “429 後在 Retry-After 前零重送.” The previous delivery message treated BL-03 as done. It is not done for live traffic. The phase-0 service is a sibling (`README.md` is honest about that); a sidecar that LiteLLM never talks to cannot honour the provider’s `Retry-After`.
- **Fix:** JobRunner must go through `GatewayRouter.execute` / `allow()` and must report events under the *provider* id, not the alias. For the real gateway, implement a LiteLLM callback or a proxy wrapper that records `Retry-After` and skips open deployments — with a test that a 429 with `Retry-After: 60` produces zero re-sends for 60s on the path clients actually use. In-memory state is acceptable for phase 0 if documented; Redis is M1.2, not a phase-0 must, but “zero re-sends” on the proxy is.

#### H4. `DataVaultRegistry` has no hospital allow-list; vault ids are globally squat-able; a signature never expires

- **Severity:** High *if this contract is treated as the medical-layer starting point*; the PR correctly says do not deploy. Still needs to change before any testnet deploy, and the tests should already encode the intended invariants.
- **Where:** `contracts/src/DataVaultRegistry.sol:53-88` (`registerVault`), `:31-32` and `:161-168` (EIP-712 type with no deadline), `:65` (global `vaultId` uniqueness)
- **Issue:** Extra Foundry probes, all passing (i.e. the bugs are real):

  1. **Self-attested hospital.** `hospital` is any address that produced a matching signature. An attacker (or a patient) can generate a key, pass it as `hospital`, and `verifyProvenance` returns true. Provenance means “this key signed this hash,” not “a hospital signed it.” The contracts README lists an allow-list as a later phase; the interface has no hook for one.
  2. **Vault-id squatting.** `vaultId` is a global key, chosen by the registrant. An attacker registers a well-known id (e.g. `keccak256("national-id-…")`) first with a self-signed hospital; the legitimate patient’s later `registerVault` reverts `VaultAlreadyExists`. The official tests do not cover this.
  3. **No deadline in `HospitalProvenance`.** A signature I produced was still valid after `vm.warp(+10 years)`. A leaked or old hospital key cannot be time-bounded. The design’s failure table (8.12) calls for signature timestamps and a revocation list.
- **Fix:** Namespace ids (`keccak256(owner, vaultId)` or `msg.sender`-scoped maps); require `hospital` to be in a governance-controlled allow-list (even a constructor-set one-address list is enough for a skeleton); add `deadline` to the EIP-712 struct and reject `block.timestamp > deadline`. Tests for squat, self-attestation (must fail), and expired signatures.

---

### Medium

#### M1. Half-open probe can get stuck; in-flight refusal reports `retry_after=0`

- **Severity:** Medium
- **Where:** `muse/gateway/circuit_breaker.py:107-111` (`_sync`), `:161-165` (`allow`), `:178` (`on_response` clears the flag)
- **Issue:** Confirmed with `FakeClock`. `allow()` in `half_open` sets `probe_in_flight=True` and returns. If the caller never calls `on_response` / `on_transport_error` (hang, killed greenlet, uncaught `BaseException`), the provider stays half-open with the flag set **forever** — advancing the clock 1000s does not help, because `_sync` only clears the flag on an `OPEN → HALF_OPEN` transition, and the state is already `HALF_OPEN`. The second `allow()` raises `CircuitOpen` with `retry_after=0` (the old `retry_at` is in the past). Clients that honour that value will spin. Concurrent first probes are correctly serialized (20 threads → 1 allowed).
- **Fix:** Probe timeout: if `probe_in_flight` and `now` is past `retry_at + probe_timeout`, clear the flag or re-open with a backoff. When refusing a second probe, report a positive retry (e.g. remaining probe timeout), not 0. Test the hung-probe case; today’s `test_only_one_probe_in_half_open` does not.

#### M2. Startup mainnet guard is a case-sensitive denylist, not an allow-list

- **Severity:** Medium (verifier still holds)
- **Where:** `muse/config.py:55-63`; `muse/settlement/constants.py:65-71` (`normalize_network` returns raw case when not a Sepolia alias), `:73-74`
- **Issue:** `Settings.__post_init__` refuses only if the *normalized* value is in `MAINNET_NETWORKS` (all-lowercase). `normalize_network("Mainnet")` returns `"Mainnet"`, which is not in the set. Probes: `Mainnet`, `BASE`, `Ethereum`, `EIP155:8453`, `base-mainnet`, `optimism`, `eip155:10` all **start**. The service will then quote Sepolia USDC anyway (`usdc_sepolia_option` is hardcoded) and `MockVerifier.verify` lowercases and refuses mainnet payloads. So this is not a funds bypass. It is a defence-in-depth failure that tests (`test_settings_refuse_mainnet_startup`) only check lowercase canonical ids for.
- **Fix:** Startup must use the same rule as the verifier: refuse unless `is_allowed_network(...)` (allow-list of Base Sepolia aliases). Drop the denylist-as-primary-guard. Test `Mainnet`, `EIP155:8453`, and `base-mainnet`.

#### M3. Consent / access semantics do not match the four-tier story

- **Severity:** Medium
- **Where:** `contracts/src/DataVaultRegistry.sol:143-150`, `:100-106`
- **Issue:** Extra probes:

  1. `accessExpiresAt` is only consulted when `tier == TimedAccess`. A `FullTransfer` registered with a short `accessExpiresAt` stays accessible after that time.
  2. After a `TimedAccess` expiry, the owner can `setTier(Query, 0)` with no new hospital signature and `isAccessible` becomes true again.
  3. `consentExpiresAt == 0` means forever (including +100 years); `registerVault` accepts a consent expiry already in the past, leaving `consentActive == true` and `isAccessible == false`.
  4. `revoke` is permanent and correctly flips access off, but leaves `dataHash`, `owner`, and `hospitalSignature` in storage and in events — expected on a public chain, and in tension with GDPR erasure (the design already says this; the official `test_storesHashNotData` does not actually prove “no personal data,” see T1).
  5. There is no ownership transfer, which the design (8.4, table 35) lists as a required property (“病人可把合約控制權轉到新錢包”).
  6. `isAccessible` is a view. Nothing in the contract enforces access; a buyer who ignores it is unconstrained. Fine for a record-keeping skeleton if that is explicit.
- **Fix:** Decide and test: does `accessExpiresAt` apply to every tier, or only TimedAccess? Should `setTier` out of an expired TimedAccess require a fresh consent event? Reject past `consentExpiresAt` at registration unless it is 0. Document that revoke is a flag, not erasure, and that enforcement is off-chain.

#### M4. Architectural deviation from the design’s medical contracts

- **Severity:** Medium (design consistency)
- **Where:** `contracts/src/*` vs design plan 8.4, 8.11, BL-30
- **Issue:** The design’s medical layer is four contracts: `IConsentRegistry` (rich state machine), per-person `IDataRightsVault` (EIP-1167 clones, salted asset commitments, grants per cohort), `ICohortPool`, `IAccessGrant`. This PR ships a single global `DataVaultRegistry` mapping, unsalt-enforced `bytes32 dataHash`, a boolean consent flag, and no cohort. A single registry is a simpler and cheaper skeleton — that can be the right phase-0 choice — but `ARCHITECTURE.md` presents it as “the per-person data vault described in the … design,” which oversells the match. Storing the raw 65-byte signature (and returning it from `vault()`) costs gas (~214k to register, ~20k to read) for a value already verified at write time; a `bool provenanceOk` plus hospital address is enough.
- **Fix:** In `ARCHITECTURE.md` and the contracts README, name the deviations and the intended successor (`IDataRightsVault` clone, salted commits, cohort grants). Stop storing the signature bytes. Do not let this registry’s shape freeze into phase 2/3 without a deliberate decision.

#### M5. 4xx client errors open the circuit; JobRunner records aliases as providers

- **Severity:** Medium
- **Where:** `muse/gateway/circuit_breaker.py:186-204`; `muse/useful_work/runner.py:144-151`, `:185-186`; `muse/gateway/router.py:75-76`
- **Issue:** Two consecutive 401/404 responses open the circuit (probe: confirmed). A bad request or a wrong key then takes the provider dark for the default cooldown. `open_after_fails` should apply to 5xx and transport errors, not to 4xx other than 429. Separately, the runner’s `report_event(model, ...)` uses `smart`/`fast`/`cheap`, and `GatewayRouter.report_event` appends unknown names to `providers`. Health endpoints then show alias names as if they were deployments.
- **Fix:** Only 429/503 (and optionally consecutive 5xx / timeouts) should open. Map aliases to underlying providers when recording, or keep a separate alias counter.

#### M6. Mock Lean “known-good” matching is a substring, so the mock is looser than it claims

- **Severity:** Medium (test / mock integrity, not a real-Lean soundness issue)
- **Where:** `muse/useful_work/lean.py:227-230`, `:26-45` (`KNOWN_GOOD` includes bare `"simp"` and `"rfl"` for `sample.nat_add_zero`)
- **Issue:** `_tactics_match` is `left == right or expected.lower() in cleaned.lower()`. The mock accepted `simp [I_made_this_up]`, `rfl_and_garbage`, and `native_decide; rfl` as verified proofs of `nat_add_zero`. Combined with listing those results for sale (and H2), CI can stamp a “verified” hash on garbage. The mock is supposed to be conservative (`docs/LEAN.md`: “a hallucinated proof must not receive a verified hash”).
- **Fix:** Exact match after whitespace normalization only. Remove bare `"simp"` or require the full tactic string. Test a near-miss proof is rejected.

#### M7. Auth and exposure footguns

- **Severity:** Medium
- **Where:** `muse/server.py:148-156`; `docker-compose.phase0.yml` (`MUSE_PHASE0_HOST: "0.0.0.0"`); `muse/config.py` (`master_key` defaults to empty)
- **Issue:** If `LITELLM_MASTER_KEY` is unset, every route including `POST /v1/providers/events` (can trip or close circuits) and the job/result APIs is open. A probe confirmed 200s with an empty key. Default bind is `127.0.0.1`, which is the right default; compose maps `${MUSE_BIND:-127.0.0.1}:4100` but sets the in-container host to `0.0.0.0`. Setting `MUSE_BIND=0.0.0.0` without a master key on a public VM exposes the stub, including mock-paid proofs. `/docs` and `/openapi.json` are in the auth allow-list even when a master key is set. Bearer comparison is not constant-time (Low, listed here only as related).
- **Fix:** Refuse to start (or refuse the mutating/sale routes) if bind is not loopback and `master_key` is empty. Keep `/docs` behind the same auth as the API. Document this next to the compose file.

#### M8. Synchronous jobs are a denial-of-service vector; no rate limit

- **Severity:** Medium
- **Where:** `muse/server.py:220-226` (`def work_jobs`, calls `runner.run` inline); Lean timeout 30s
- **Issue:** `POST /v1/work/jobs` runs the whole chain on a FastAPI sync worker. In real mode that is a 30s (times number of aliases) block; even in mock mode, an unauthenticated (M7) or authenticated client can fill the process. There is no queue, no concurrency cap, no job timeout at the HTTP layer.
- **Fix:** For phase 0, a simple in-process semaphore and a short timeout are enough. Do not leave this as the shape of the production runner.

---

### Low

#### L1. Tautological and missing tests (see also “Test quality”)

- **Where:** `contracts/test/DataVaultRegistry.t.sol` `test_storesHashNotData` (asserts a `bytes32` is 32 bytes); no tests for B1, B2, H2 replay, H3 integration, M1 hung probe, M2 case, H4 squat
- **Fix:** Replace `test_storesHashNotData` with a source-level check (no function argument of type `string` / unbounded `bytes` for payload — the signature `bytes` is a counterexample you should justify). Add the missing negative tests.

#### L2. `forge install` in CI is unpinned in the command line

- **Where:** `.github/workflows/ci.yml` (`foundry-rs/foundry-toolchain@v1` with no version; `forge install foundry-rs/forge-std --no-commit` without `@v1.17.0`). `foundry.lock` *is* committed at `v1.17.0` / `f3dae6e6…`.
- **Fix:** `forge install foundry-rs/forge-std@v1.17.0 --no-commit` and pin the toolchain (e.g. `version: "1.9.0"`). Green CI today is not a reproducibility guarantee.

#### L3. Retry-After parser quirks (not wrong for RFC 9110, worth knowing)

- **Where:** `muse/gateway/retry_after.py:39-54`
- **Issue:** `isdigit()` accepts fullwidth digits (`３０` → 30). An HTTP-date in the past yields delay 0, so a 429 with a stale date goes half-open immediately (no protection). RFC 850 / asctime forms are accepted via `parsedate_to_datetime` and then capped — fine. `"30, 60"` falls back to the default — fine. Negative `"-5"` uses the default — fine.
- **Fix:** Optional: treat a past HTTP-date as “use default cooldown,” not zero. Document it.

#### L4. Small leaks and hygiene

- `list_problems` returns local filesystem `path` (`muse/useful_work/runner.py:88-95`).
- `ResultStore._flush` is a non-atomic `write_text` (torn file on crash).
- Exception middleware returns redacted `str(exc)` to the client (internal paths can still leak).
- `HealthStore.last_error` can persist a provider error string (usually safe; do not put headers there).
- Design-plan market figures and legal citations are out of scope for this PR; `ARCHITECTURE.md` correctly marks regulations as “not an opinion.”

#### L5. Word design plan is stale relative to PR #2 (expected, but claims need care)

The plan was written against PR #1: “尚未開始：第 2–4 層,” “現行冷卻是固定 30 秒,” BL-03 still P0. That is accurate for the *proxy*. It is misleading if cited as “PR #2 finished BL-03 / BL-13.” Update the plan’s §1.4 / §4.2 / backlog in a later docs pass; do not treat this PR as having closed those items (see H3, B1, B2).

---

## Test quality

The official suite is **better than tautological on the happy paths it covers**, and **silent on the invariants that matter**.

| Area | What the tests actually prove | What they miss |
| --- | --- | --- |
| Circuit breaker (12 tests) | Delta-seconds, one IMF-fixdate, missing/garbage → default, cap, 429/503 open, success closes, half-open failure takes a new header, 5xx threshold, one probe | Hung probe (M1), `retry_after=0`, concurrent probes, past HTTP-date, 4xx opening the circuit, **integration with JobRunner (H3)** |
| Fallback (6 tests) | First success, skip open, 429 trips and continues, exhausted, empty chain, usage not double-counted | — adequate for the unit |
| x402 (10 tests) | Sepolia USDC quote shape, mock accepted with `settled: false`, insufficient / wrong payee / non-mock / lowercase mainnet / live facilitator refused | Replay, cross-resource, `validBefore`, `scheme`, startup case aliases (M2), job-API leak (H2) |
| Useful work (7 tests) | Samples load, mock accepts a known-good and rejects `sorry`, hash is stable, LLM/proof fallback | Statement swap, axioms, substring `simp`, sandbox, real-mode assembler |
| HTTP service (4 tests) | Keys do not echo secrets, events open a circuit, unpaid 402 then mock pay 200, demo seeded | Proof already present on the 201, GET job bypass, replay |
| Secrets sweep (3 tests) | `.gitignore`, empty `.env.example` keys, a few forbidden snippets | Narrow; skips `tests/` and does not fail on `BASE_MAINNET_USDC` appearing as a denylist (correct, but the sweep is not a security proof) |
| Foundry (15 tests) | Valid / invalid / wrong-hash signatures, zero ids, duplicate id, only-owner, permanent revoke, four tiers, timed + consent expiry, pause without revoke, unknown vault, 256-run owner fuzz | Squatting, self-hospital, no deadline, `setTier` escape, `accessExpiresAt` on other tiers, subject mismatch (I added these out-of-repo; they needed to be in the PR). `test_storesHashNotData` is tautological. `test_registerEmitsEvents` only checks `VaultRegistered`. |

**59 green tests do not mean 59 meaningful guarantees.** Several tests assert the mock’s own conventions (known-good strings, `settled is False`) rather than the properties the design will need later (statement lock, unique payment, skip-open-circuit on the runner). That is the main test-quality problem.

---

## CI

- Four jobs: existing `validate` and `boot` unchanged; new `phase0-python` and `phase0-contracts`. Permissions `contents: read`. Good.
- `phase0-python` uses `requirements-dev.txt` and `pytest tests -q`. Lean is mock-only, as documented. It will not catch B1/B2.
- `phase0-contracts` installs forge-std on the fly (L2) and runs `forge test -vv`. Locally: 15 passed, fuzz 256. GitHub: passed in ~15s.
- `boot` still does not start the phase-0 service. A compose misconfiguration of `MUSE_X402_NETWORK` or a missing refuse-mainnet check would not be caught by `boot`. Optional follow-up: a fifth job that boots `python -m muse` with `TestClient` or a curl of `/health`.
- No job runs the extra negative probes in this review. They should become official tests.

---

## Docs vs design plan vs code

Honest in some places, ahead of the code in others.

| Claim | Reality |
| --- | --- |
| PR body: “Circuit breaker that honours Retry-After … fallback chains that skip an open circuit” | True for the *library*. False for LiteLLM :4000 and for `JobRunner` (H3). |
| PR body / ARCHITECTURE: “Startup refuses a mainnet network” | True for lowercase canonical names; false for `Mainnet`, `EIP155:8453`, `base-mainnet` (M2). Verifier still refuses. |
| ARCHITECTURE: job GET returns “status, verdict, result hash” | Code also returns `proof` (H2). |
| ARCHITECTURE / LEAN.md: mock is conservative; real mode is exit 0 and no `sorry` | Mock accepts any proof containing `simp` or `rfl` (M6). Real mode is unsound (B1, B2) and unsandboxed (H1). LEAN.md should say this is **not** a verifier yet. |
| ARCHITECTURE: “the per-person data vault described in the design” | A single registry, not EIP-1167 per-person vaults, no cohort, no salted commitment enforcement (M4). |
| Design 6.4 / BL-13: statement-hash lock, axiom whitelist, dual replay, no-network container | Hash is computed and stored; none of the locks exist. |
| Design 4.5 / M1.2 / BL-03: Redis-backed breaker in front of or inside LiteLLM, daily/weekly EXHAUSTED state, jitter | In-memory, three states, no jitter, not in the proxy. Acceptable for a sketch if BL-03 stays open. |
| Design 5.2: x402 + EIP-3009, per-resource, nonce | Mock only, and the mock ignores nonce, expiry, and resource (H2). Correctly refused as a live facilitator. |
| Design 8.11 virtual Solidity | Different types and richer consent states. Fine for phase 0 if labelled a deviation. |
| Regulations table in ARCHITECTURE | Appropriately hedged. Do not let “the individual registers their own vault” be read as a legal conclusion (the design already says this needs counsel). |

---

## Architectural concerns: is this the right foundation?

**Partly, for a spike; not for the system described in the design plan.**

What is the right call:

- A sibling service on :4100, mock-only settlement, no deploy path, and leaving the PR #1 proxy CI alone. That is a safe way to experiment without risking the gateway.
- USDC on Base Sepolia, no self-issued token, hashes rather than payloads on chain, hospital as signer and patient as `msg.sender`. Those product decisions match the plan and should stay.
- Foundry tests and a secrets sweep from day one.

What is the wrong shape to freeze:

1. **Two HTTP stacks that do not share a breaker.** If Muse Stack’s selling point is “we honour Retry-After so we do not burn free-tier quota,” that logic has to live on the LiteLLM path (callback or wrapper), not only in a Python library the proxy never imports. The current split will become a permanent fork.
2. **A “verified” hash that is not a function of a sound check.** Once result hashes are treated as sellable objects (and eventually put on chain), unsound real-mode hashes are worse than no hashes. Keep real mode from emitting `verified` until B1/B2/H1 are done.
3. **A payment stub whose tests encode the wrong protocol** (replayable, unbound, goods delivered on a different URL). Replacing `MockVerifier` with a facilitator later will not automatically add a nonce ledger or a resource binding.
4. **A medical registry that looks complete** (four tiers, EIP-712, revoke, Foundry fuzz) but omits the allow-list, deadlines, and per-person isolation the plan’s legal story depends on. Reviewers — and later, hospital partners — will reasonably think this is the contract. Either shrink the surface (hash + owner + “do not deploy”) or implement the invariants now.
5. **In-memory, synchronous, single-process state** is fine for phase 0. Do not build phase-1 settlement and job accounting on `ResultStore` / `JobRunner._jobs` as they stand.

Recommended foundation work, in order, is the next section. After that, keep this PR stacked and unmerged, and reopen BL-03 / BL-10 / BL-13 in the plan as still open.

---

## What must change first

Before calling this a foundation, and before anyone sets `MUSE_LEAN_MODE=real` or demos a “paid” result as a product:

1. **Sound Lean (B1, B2, H1).** Pin the statement; refuse to emit `verified` from real mode until axioms are allow-listed and Lean is sandboxed without secrets. Add tests that a substituted `theorem True` and an `axiom` payload cannot produce a saleable hash.
2. **Honest payment wall (H2).** Stop returning `proof` from the job API; bind and consume a nonce per resource even in the mock; test replay and cross-result use so they fail.
3. **Breaker on the real call path (H3).** `JobRunner` must `allow()` / skip open circuits. A LiteLLM callback (or a documented, still-open BL-03) must be the plan for :4000 — do not describe BL-03 as done.

Then, before any testnet talk of `DataVaultRegistry`: H4 (allow-list, namespaced ids, signature deadline) and M3 (access semantics). M1, M2, M6, M7 are smaller and should go in the same revision as tests that would have caught this review’s probes.

I would **request changes** and leave the PR open. I would not approve, and I would not merge.
