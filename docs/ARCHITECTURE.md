# Muse Stack architecture (phase 0)

This document is the system design that the phase 0 code implements, and
the parts it only sketches. It follows the Muse Stack / Ceresus deck
(November 2026): one mechanism in four layers, owned by the individual,
verifiable, and settleable. Ceresus (a Singapore entity) is the intended
contracting and custodian body for a later medical-data pilot; this
repository does not implement that legal wrapper.

This is not legal advice. Regulatory citations are design references and
must be confirmed by counsel before any medical-data pilot.

## The four layers are one mechanism

```
 application ──► LiteLLM gateway ──► LiteLLM already honours Retry-After
                      │                      │
                      │                      ▼
                      │              Muse-side breaker + thin callback
                      │              (quota-exhausted, cross-alias)
                      │              fallback chain (free → cheap
                      │              → member keys → OpenRouter)
                      │
                      ▼
              useful-work runner ──► Lean verify (statement lock,
                                      axiom allow-list, sandbox)
                                              │
                                              ▼
                                   x402 settlement stub
                                   (HTTP 402, USDC, Base Sepolia,
                                    mock verifier, quote-bound
                                    nonce, no real funds)

 data stays off-chain, encrypted on the individual's side
 chain (later) holds only: hash, consent, tier, hospital signature hash
```

1. **Routing.** Use leftover free and cheap model quota well. Clients call
   aliases (`fast`, `smart`, `cheap`, `long-context`, `embed`, `rerank`) on
   the LiteLLM proxy from [PR #1](https://github.com/zandis/Muse-stack/pull/1).
   LiteLLM 1.108.5 already honours a provider's `Retry-After` header (see
   below). Phase 0 adds a *Muse-side* circuit breaker for useful-work
   jobs and health events, a thin LiteLLM callback only for
   quota-exhausted state and cross-alias cooldown, an explicit fallback
   chain for Muse-side calls, and a per-provider health / usage endpoint.
   Members add their own keys through the environment; secrets are never
   committed and never logged.
2. **Results settlement.** Sell a *verified result*, not a quota. An x402
   HTTP 402 handshake quotes a price in USDC on Base Sepolia. Phase 0
   ships a mock verifier only: it will not start unless the network is
   on a case-insensitive allow-list of Base Sepolia identifiers
   (`eip155:84532` and aliases such as `base-sepolia`), will not call a
   live facilitator, and never moves funds. Payments are bound to a
   quote (`authorization.nonce = SHA-256(quoteId)`), have a validity
   window, and cannot be replayed. Unsigned top-level `amount` / `payTo`
   are ignored. Mock Lean results are never sold.
3. **Useful work.** Spend leftover weekly quota on problems that can be
   checked automatically. The runner takes a Lean problem (bundled samples,
   or a local [formal-conjectures](https://github.com/google-deepmind/formal-conjectures)
   checkout), asks routed aliases for a proof, verifies it against the
   *trusted* statement (a lake-comparator-style lock: the model cannot
   replace the theorem), and records a result hash. Mock mode never
   emits `verified` and is never listed for sale. CI has no Lean
   toolchain and is fail-closed; a real `lean` binary is documented in
   [LEAN.md](LEAN.md) and runs in a sandbox.
4. **Registry (not a "data vault").** A Solidity *registry* of hashes
   and consent: `DataVaultRegistry` stores a data hash, consent, an
   access tier (`Query` / `Federated` / `TimedAccess` / `FullTransfer`),
   an allow-listed hospital's address, and the *hash* of an EIP-712
   provenance signature (the 65-byte signature itself is not stored).
   Vault ids are `keccak256(abi.encode(owner, salt))`. Foundry tests
   only; no deployment. Per-patient cohort linkage on-chain is
   **out-of-scope research** pending a privacy design — this contract
   must not be used that way.

## LiteLLM already honours Retry-After

This is a correction of an earlier claim. Muse Stack did **not** invent
Retry-After support for the proxy, and we do not re-implement it.

In LiteLLM 1.108.5 (image `ghcr.io/berriai/litellm:v1.108.5`, tag commit
`2a61f2114d45016bae77930903e65172cd8e762c`),
`Router.deployment_callback_on_failure` in `litellm/router.py` (around
lines 8918–9025) chooses a cooldown with this priority:

1. The deployment's own `cooldown_time` (`model_info` or
   `litellm_params`), if set and `>= 0`.
2. The provider's `Retry-After` header, if present and `>= 0`
   (`litellm.utils._get_retry_after_from_exception_header`).
3. The router's `cooldown_time` (`router_settings.cooldown_time`) as
   the *fallback* when there is no header.

Muse Stack's renderer sets `router_settings.cooldown_time: 30` and does
**not** set a per-deployment `cooldown_time`. That 30-second value is
therefore a fallback only: it does **not** disable Retry-After. The
header wins when the provider sends one. The same priority is applied
in `litellm/router_utils/fallback_event_handlers.py`.

Caveats that *are* still Muse's job:

- LiteLLM cooldowns are keyed by deployment id. Muse renders one
  deployment per alias as `{pin_id}__{alias}`, so a 429 on
  `groq-gpt-oss-20b__fast` does not automatically cool
  `groq-gpt-oss-20b__smart`. The thin callback
  `scripts/muse_litellm_callback.py` copies the cooldown (honouring
  the same Retry-After when present) to sibling deployments that share
  the upstream `model` string.
- A *quota-exhausted* 429 (daily/weekly cap, `insufficient_quota`, …)
  is given a long cooldown on every sibling, even if LiteLLM's
  `allowed_fails` (unchanged from PR #1, still 2) has not yet tripped
  the primary cooldown.
- The Muse-side breaker used by the useful-work runner and
  `POST /v1/providers/events` applies the same Retry-After rules
  (monotonic clock, jitter, probe timeout, 401/404 ignored, past
  HTTP-dates use the default cooldown rather than a zero delay). It
  is not a second implementation inside the proxy.

The callback is registered as
`callbacks: ["muse_litellm_callback.proxy_handler_instance"]`, which is
the form LiteLLM 1.108.5 documents. It swallows its own errors so it
cannot break the proxy. `scripts/render_and_start.py` puts `scripts/`
on `PYTHONPATH` before `execvp` so the image can import it.

## Why these choices

| Choice | Why |
| --- | --- |
| No self-issued token | Settlement is USDC, a stablecoin, so the mechanism is not a securities-like issuance. x402 is the handshake; we use it rather than compete with it. |
| Sell results, not quota | Free-tier ToS generally forbids reselling access. A Lean-verified proof is an output the individual can own and sell. Mock Lean results are not verified and are not sold. |
| Hashes, consent, and payments on-chain; data off-chain | Zero-trust: the chain is a record, not a store. A breach of a node does not leak the payload. The registry must not put per-patient cohort linkage on-chain. |
| Hospital signs provenance, patient owns the record | The hospital is an allow-listed issuer that can attest origin and revoke an attestation. The registering address is the patient. Vault ids cannot be squatted. Taiwan's Personal Data Protection Act binds the *controller*, not the data subject selling a copy they have a right to obtain (design intent; confirm with counsel). |
| Mock verifier and Base Sepolia only, in this release | Phase 0 must be safe to run in CI and on a laptop. A live facilitator would move funds. The network guard is an allow-list (`eip155:84532`), not a case-sensitive denylist. |
| Keys only in the environment | A rendered LiteLLM config uses `os.environ/NAME` references. `.env` is gitignored. `/v1/providers/keys` reports `configured` or `missing`, never a value. The service refuses to start without a master key unless it is bound to loopback (and then logs a loud warning). When a key is set, *every* endpoint including `/health` and `/docs` requires it. Compose publishes `127.0.0.1` by default. |
| Free-tier traffic is not for patient data | Unchanged from PR #1. Gemini's free tier is used to improve Google's products; other free endpoints log prompts. See the README warning. |

## Zero-trust shape (what is *not* built yet)

The deck's four lanes:

1. **Individual.** Data is encrypted under the person's keys and stays in
   a store they control (for example their own Drive). Phase 0 does not
   ship a client vault; the Solidity *registry* only stores a hash.
   Designing how a later client would keep data off-chain — and how
   *not* to put per-patient cohort linkage on-chain — is out-of-scope
   research, called out in `contracts/README.md`.
2. **Verification.** A hospital digital signature attests origin; a hash
   comparison detects tampering. Phase 0 implements the on-chain check
   (`DataVaultRegistry.verifyProvenance`), an issuer allow-list, an
   EIP-712 digest with a deadline, attestation revocation, and storage
   of the signature *hash* (not the 65-byte signature). It does not
   issue hospital certificates or talk to a hospital system.
3. **Chain.** Only hashes, consent, and (later) payment records. The
   registry is the skeleton. Payments are *not* on-chain in phase 0;
   the x402 stub issues a mock receipt with `settled: false`.
4. **Buyer.** Federated learning or a secure compute environment, with
   an audit trail. Not built. Access tiers are recorded so a later
   phase can enforce them. `accessExpiresAt` applies to every tier
   whenever it is non-zero; `setTier` cannot clear, extend, or revive
   an expired window without a new hospital signature.

## HTTP surface (phase 0 service)

Default bind: `127.0.0.1:4100`. The process refuses to start if
`LITELLM_MASTER_KEY` is unset and the bind is not loopback. When the
key is set, **every** path — including `/health`, `/docs`, and
`/openapi.json` — requires `Authorization: Bearer <that key>`
(compared with `hmac.compare_digest`). Docker Compose publishes the
host port on `127.0.0.1` by default; the in-container bind is
`0.0.0.0` and therefore requires a master key.

App construction does **not** run a job and does **not** seed a demo
result. Job endpoints never return a proof (that is the paid good).
Problem listings never include a local filesystem path. Sales are
recorded only after the paid response has been built and declared
safe (a Starlette `BackgroundTask`). Jobs run in a worker thread
behind a concurrency cap and return 429 when busy.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Liveness. Reports network `eip155:84532` and facilitator `mock`. Authenticated when a master key is set. |
| `GET` | `/v1/providers/health` | Per-provider circuit state (`closed` / `open` / `half_open` / `exhausted`), `Retry-After`, usage. |
| `GET` | `/v1/providers/usage` | The same counters, slimmer. |
| `GET` | `/v1/providers/keys` | Which documented env vars are `configured` or `missing`. Never values. |
| `POST` | `/v1/providers/events` | Ingest a 429 / success / failure from a sidecar or test. Honours `retryAfter`. A malformed value (including a past-looking non-date) returns 400; exception text is not leaked. |
| `GET` | `/v1/work/problems` | Bundled samples, plus formal-conjectures if configured. No local paths. |
| `POST` | `/v1/work/jobs` | Run a job (async, rate-limited). The body never includes a proof. Only a *real* Lean verification is listed for sale; mock results return 200 with `listedForSale: null`. |
| `GET` | `/v1/work/jobs/{id}` | Job status, verdict, result hash. The proof is always withheld. |
| `GET` | `/v1/results` | Catalogue (proofs withheld). Empty at startup. |
| `GET` | `/v1/results/{id}` | **HTTP 402** with a quote (`quoteId`, expiry, bound nonce) when unpaid; **200** plus the proof and a mock receipt when `PAYMENT-SIGNATURE` (or v1 `X-PAYMENT`) is a valid mock payload bound to that quote. A malformed header returns 400. |

A 402 response carries a v1-compatible body (`x402Version: 1`,
`accepts[].network = base-sepolia`, Circle USDC on Base Sepolia,
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`) and a v2
`PAYMENT-REQUIRED` header (CAIP-2 `eip155:84532`), plus the issued
quote. The stub accepts v2 `PAYMENT-SIGNATURE` and v1 `X-PAYMENT`.
See the Coinbase x402 migration guide for the header and network-id
changes.

A mock payment is a base64 JSON body with `"mock": true` or
`"signature": "0xmock"`, `scheme` exactly `exact`, a *signed*
`authorization.to` / `authorization.value` / `authorization.nonce`
matching the quote (`nonce = SHA-256(quoteId)`), a validity window
containing now, and an unspent nonce. Unsigned top-level `amount` and
`payTo` are ignored. The signed amount must *equal* the quote, not
merely meet it. `muse.settlement.mock_payment.make_mock_payment`
builds one. The receipt always has `settled: false`.

## Routing details (Muse-side breaker)

LiteLLM's own Retry-After behaviour is described above. The Muse-side
`CircuitBreaker` (used by the useful-work runner and the events API)
adds:

- A `429` or `503` opens the circuit until the header's delta-seconds
  or HTTP-date (RFC 9110), capped at one hour, with a small jitter so
  callers do not retry in a herd. A missing header uses
  `MUSE_DEFAULT_COOLDOWN_SECONDS` (default 30). An unparseable header
  on the *events API* is HTTP 400; a bad header on a *provider
  response* falls back to the default. A past HTTP-date uses the
  default cooldown, not a zero delay.
- After `retry_at`, the circuit is `half_open` and a single probe is
  allowed. The probe has a timeout; a hung probe re-opens with a
  positive `retry_after` (never 0). Success closes it only if it
  carries the current generation — a late success cannot close a
  circuit that has since re-opened.
- Other `5xx` open the circuit after `open_after_fails` (default 2)
  consecutive failures. Other `4xx`, including `401` and `404`, do
  **not** open the circuit. Only `429` (and quota-exhausted) among
  the 4xx class does.
- A quota-exhausted 429 goes to an `exhausted` state with a long
  cooldown, shared across aliases that use the same upstream model.
- `FallbackChain` skips open circuits, records the failure, and tries
  the next provider. `GatewayRouter` exposes that plus usage counters.
  When the runner uses an alias (`smart`), the breaker key is the
  upstream model id so sibling aliases cool together.

The useful-work runner walks LiteLLM *aliases* (`smart`, then `fast`,
then `cheap`) through the breaker. A rejected proof moves on to the
next alias and does **not** open a provider circuit. A 429 from the
proxy does, under the upstream key.

Members add keys by copying `.env.example` to `.env`. The proxy
renderer drops any provider whose variable is empty. That behaviour
is unchanged and is still checked by `scripts/validate_config.py`.

## Lean verification (lake-comparator style)

See [LEAN.md](LEAN.md). The short version:

- The trusted statement comes from the problem file, never from the
  model. A proof that starts with `theorem x : True := trivial` (or
  any other replacement) is rejected in the assembler, before Lean
  runs. The assembled file is the trusted preamble and theorem with
  the hole filled.
- Banned constructs (`sorry`, `admit`, `native_decide`,
  `ofReduceBool`, `implemented_by`, `debug.skipKernelTC`, `extern`,
  axiom declarations, elaboration-time IO) are rejected by a
  comment-aware scan.
- In real mode, Lean runs in a sandbox: scrubbed environment (no
  provider keys), no network (`bwrap --unshare-net` when
  bubblewrap is available), a temporary directory, CPU / memory /
  file rlimits, and a timeout. Production deployments should put
  this inside gVisor, Firecracker, or bubblewrap; the code uses
  bubblewrap when it is on `PATH` and documents the others.
- After Lean exits 0, `#print axioms` must be parseable and limited
  to `propext`, `Quot.sound`, and `Classical.choice`, and `#check`
  must mention the trusted constant. Anything else is fail-closed
  (including a missing Lean binary).
- Mock mode runs the same static checks but always returns
  `verified: false, mode: mock` and is never listed for sale. It
  does not accept a proof merely because the text mentions a known
  tactic.

## Regulations (design response, not an opinion)

| Instrument | Design response in this repo |
| --- | --- |
| Taiwan PDPA Arts. 2 and 6 | The individual registers their own record. The hospital is an allow-listed signer, not the owner. Special-category data is not processed by this code. |
| Taiwan Medical Care Act Art. 72 | Medical records stay with the institution; the registry stores a hash of a copy the patient obtained through their right of access. |
| Taiwan Human Subjects Research Act | A later medical pilot needs IRB review. Not started. |
| GDPR Art. 9 | Special-category data needs an explicit lawful basis; consent is recorded and can be revoked on-chain. |
| HIPAA 45 CFR 164.524 | Right of access is the intended source of the copy. This stack is not a HIPAA system and signs no BAA. |
| Singapore PDPA | Ceresus as a Singapore entity is a later contracting wrapper. Not implemented in code. |

Any medical-data pilot also has to deal with pathology-report copyright
and relatives' genetic privacy, which this skeleton does not address.
Putting per-patient cohort linkage on-chain is explicitly out of scope.

## What works, what is stubbed

**Works, with tests:**

- LiteLLM Retry-After documented from the 1.108.5 source; Muse-side
  breaker (generation tickets, probe timeout, jitter, monotonic clock,
  401/404 ignored, quota-exhausted, cross-alias); thin LiteLLM callback
  for sibling cooldowns; fallback chains; health / usage / key-status.
- x402 402 ↔ quote-bound mock-pay ↔ 200 handshake for a *real*
  verified result, Base Sepolia USDC allow-list, replay and expiry
  protection, signed fields only, `scheme == exact`, exact amount,
  sale recorded after delivery, malformed headers return 400.
- Useful-work runner on bundled Lean samples and formal-conjectures
  fixtures (imports, comments, unique ids), mock LLM, mock Lean that
  never verifies, statement lock, sandbox, jobs that do not leak a
  proof or a local path, no job at app construction, concurrency cap.
- `DataVaultRegistry` issuer allow-list, owner-bound vault ids, EIP-712
  deadline, signature-hash storage, attestation revocation,
  `accessExpiresAt` on every tier, `setTier` cannot clear / extend /
  revive expired access, Foundry tests including fuzz on owner checks
  and vault-id binding.
- Auth: refuse to start without a master key off loopback; all
  endpoints authenticated when a key is set; compose bind 127.0.0.1.
- Secret redaction, empty-key catalog validation (PR #1), gitignore
  for `.env` and Foundry `broadcast/`. Foundry CI pins
  `foundry-toolchain` 1.9.0 and `forge-std` v1.17.0.

**Stubbed or explicit non-goals of this phase:**

- Live x402 facilitator, EIP-3009 verification, and any on-chain
  settlement. The mock never talks to RPC.
- Real Lean in CI. Documented; enabled locally with `MUSE_LEAN_MODE=real`
  when `lean` is on `PATH`. Without Lean, real mode is fail-closed.
- Vendoring formal-conjectures (not vendored; a local checkout and
  test fixtures are enough).
- Client-side encrypted store, hospital certificate issuance,
  federated learning, secure compute, **on-chain cohort linkage**
  (out of scope), IRB workflow.
- Deployment of the Solidity contract (no script, no RPC, no mainnet).
- Billing of any kind (no Stripe, no invoices, no self-issued token).
- Putting patient data through the free-tier gateway.
- gVisor / Firecracker isolation in CI (bubblewrap is used when
  present; the others are the documented production path).

## Running it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests -q
python scripts/validate_config.py
# Foundry (optional on a laptop; required in CI):
#   curl -L https://foundry.paradigm.xyz | bash && foundryup
cd contracts && forge install foundry-rs/forge-std@v1.17.0 --no-commit && forge test -vv

# Phase 0 service, mock LLM and mock Lean, no provider keys required,
# but a master key is required unless you bind to loopback:
LITELLM_MASTER_KEY=sk-dev python -m muse          # http://127.0.0.1:4100
curl -s -H "Authorization: Bearer sk-dev" http://127.0.0.1:4100/health

# LiteLLM proxy (unchanged from PR #1):
cp .env.example .env    # set LITELLM_MASTER_KEY and any provider keys
docker compose up -d    # http://127.0.0.1:4000
```

To have the runner ask the live proxy for proofs, set
`MUSE_WORK_LLM=litellm` and point `MUSE_BASE_URL` at the proxy. That
path is not exercised in CI and will spend quota.

## Next steps (not in this PR)

- Wire a *testnet* x402 facilitator (Base Sepolia only) behind an explicit
  flag, still refusing anything off the Sepolia allow-list.
- Optional real Lean in CI via a cached elan install, or a dedicated job,
  still sandboxed (gVisor / Firecracker / bubblewrap).
- A formal-conjectures pin and a problem-selection policy.
- Testnet deploy of the registry (deliberate, reviewed), and an off-chain
  client that keeps data off-chain. Privacy review before any cohort
  design — no per-patient cohort linkage on-chain.
- The Ceresus contracting wrapper, after counsel review.
- Keep PR #1 and this PR unmerged until you say otherwise.
