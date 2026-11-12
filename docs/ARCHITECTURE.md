
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
 application ──► LiteLLM gateway ──► circuit breaker (Retry-After)
                      │                      │
                      │                      ▼
                      │              fallback chain (free → cheap
                      │              → member keys → OpenRouter)
                      │
                      ▼
              useful-work runner ──► Lean verify ──► result hash
                                              │
                                              ▼
                                   x402 settlement stub
                                   (HTTP 402, USDC, Base Sepolia,
                                    mock verifier, no real funds)

 data stays off-chain, encrypted on the individual's side
 chain (later) holds only: hash, consent, tier, hospital signature, payment
```

1. **Routing.** Use leftover free and cheap model quota well. Clients call
   aliases (`fast`, `smart`, `cheap`, `long-context`, `embed`, `rerank`) on
   the LiteLLM proxy from [PR #1](https://github.com/zandis/Muse-stack/pull/1).
   Phase 0 adds a circuit breaker that honours `Retry-After`, an explicit
   fallback chain for Muse-side calls, and a per-provider health / usage
   endpoint. Members add their own keys through the environment; secrets
   are never committed and never logged.
2. **Results settlement.** Sell a *verified result*, not a quota. An x402
   HTTP 402 handshake quotes a price in USDC on Base Sepolia. Phase 0
   ships a mock verifier only: it will not start on a mainnet network,
   will not call a live facilitator, and never moves funds.
3. **Useful work.** Spend leftover weekly quota on problems that can be
   checked automatically. The runner takes a Lean problem (bundled samples,
   or a local [formal-conjectures](https://github.com/google-deepmind/formal-conjectures)
   checkout), asks routed aliases for a proof, verifies it, and records a
   result hash. CI mocks the Lean toolchain; a real `lean` binary is
   documented in [LEAN.md](LEAN.md).
4. **Data market.** A Solidity registry for a per-person data vault:
   hash, consent, access tier (`Query` / `Federated` / `TimedAccess` /
   `FullTransfer`), and a hospital EIP-712 provenance signature. Foundry
   tests only; no deployment.

## Why these choices

| Choice | Why |
| --- | --- |
| No self-issued token | Settlement is USDC, a stablecoin, so the mechanism is not a securities-like issuance. x402 is the handshake; we use it rather than compete with it. |
| Sell results, not quota | Free-tier ToS generally forbids reselling access. A Lean-verified proof is an output the individual can own and sell. |
| Hashes, consent, and payments on-chain; data off-chain | Zero-trust: the chain is a record, not a store. A breach of a node does not leak the payload. |
| Hospital signs provenance, patient owns the vault | The hospital is a custodian that can attest origin. The registering address is the patient. Taiwan's Personal Data Protection Act binds the *controller*, not the data subject selling a copy they have a right to obtain (design intent; confirm with counsel). |
| Mock verifier and Base Sepolia only, in this release | Phase 0 must be safe to run in CI and on a laptop. A live facilitator would move funds. |
| Keys only in the environment | A rendered LiteLLM config uses `os.environ/NAME` references. `.env` is gitignored. `/v1/providers/keys` reports `configured` or `missing`, never a value. |
| Free-tier traffic is not for patient data | Unchanged from PR #1. Gemini's free tier is used to improve Google's products; other free endpoints log prompts. See the README warning. |

## Zero-trust shape (what is *not* built yet)

The deck's four lanes:

1. **Individual.** Data is encrypted under the person's keys and stays in
   a store they control (for example their own Drive). Phase 0 does not
   ship a client vault; the Solidity registry only stores a hash.
2. **Verification.** A hospital digital signature attests origin; a hash
   comparison detects tampering. Phase 0 implements the on-chain check
   (`DataVaultRegistry.verifyProvenance`) and the EIP-712 digest a
   hospital would sign. It does not issue hospital certificates or talk
   to a hospital system.
3. **Chain.** Only hashes, consent, and (later) payment records. The
   registry is the skeleton. Payments are *not* on-chain in phase 0;
   the x402 stub issues a mock receipt with `settled: false`.
4. **Buyer.** Federated learning or a secure compute environment, with
   an audit trail. Not built. Access tiers are recorded so a later
   phase can enforce them.

## HTTP surface (phase 0 service)

Default bind: `127.0.0.1:4100`. If `LITELLM_MASTER_KEY` is set, every
path except `/health` requires `Authorization: Bearer <that key>`.

| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/health` | Liveness. Reports network `eip155:84532` and facilitator `mock`. |
| `GET` | `/v1/providers/health` | Per-provider circuit state (`closed` / `open` / `half_open`), `Retry-After`, usage. |
| `GET` | `/v1/providers/usage` | The same counters, slimmer. |
| `GET` | `/v1/providers/keys` | Which documented env vars are `configured` or `missing`. Never values. |
| `POST` | `/v1/providers/events` | Ingest a 429 / success / failure from a sidecar or test. Honours `retryAfter`. |
| `GET` | `/v1/work/problems` | Bundled samples, plus formal-conjectures if configured. |
| `POST` | `/v1/work/jobs` | Run a job synchronously. A verified result is listed for sale. |
| `GET` | `/v1/work/jobs/{id}` | Job status, verdict, result hash. |
| `GET` | `/v1/results` | Catalogue (proofs withheld). |
| `GET` | `/v1/results/{id}` | **HTTP 402** without a payment; **200** plus the proof and a mock receipt when `PAYMENT-SIGNATURE` (or v1 `X-PAYMENT`) is a valid mock payload. |

A 402 response carries a v1-compatible body (`x402Version: 1`,
`accepts[].network = base-sepolia`, Circle USDC on Base Sepolia,
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`) and a v2
`PAYMENT-REQUIRED` header (CAIP-2 `eip155:84532`). The stub accepts
v2 `PAYMENT-SIGNATURE` and v1 `X-PAYMENT`. See the Coinbase x402
migration guide for the header and network-id changes.

A mock payment is a base64 JSON body with `"mock": true` or
`"signature": "0xmock"`, the quoted `payTo`, the Sepolia USDC asset,
and an amount of at least the quote. `muse.settlement.mock_payment.make_mock_payment`
builds one. The receipt always has `settled: false`.

## Routing details

LiteLLM (PR #1) already walks deployments inside an alias with
`order`, `allowed_fails`, and a *fixed* `cooldown_time`. It does not
read a provider's `Retry-After` header. The phase 0 breaker does:

- A `429` or `503` opens the circuit until the header's delta-seconds
  or HTTP-date (RFC 9110), capped at one hour. A missing or bad header
  uses `MUSE_DEFAULT_COOLDOWN_SECONDS` (default 30).
- After `retry_at`, the circuit is `half_open` and a single probe is
  allowed. Success closes it; failure re-opens with a new header if
  present.
- Other `5xx` open the circuit after `open_after_fails` (default 2)
  consecutive failures.
- `FallbackChain` skips open circuits, records the failure, and tries
  the next provider. `GatewayRouter` exposes that plus usage counters.

The useful-work runner walks LiteLLM *aliases* (`smart`, then `fast`,
then `cheap`). A rejected proof moves on to the next alias and does
**not** open a provider circuit. A 429 from the proxy does.

Members add keys by copying `.env.example` to `.env`. The proxy
renderer drops any provider whose variable is empty. That behaviour
is unchanged and is still checked by `scripts/validate_config.py`.

## Regulations (design response, not an opinion)

| Instrument | Design response in this repo |
| --- | --- |
| Taiwan PDPA Arts. 2 and 6 | The individual registers their own vault. The hospital is a signer, not the owner. Special-category data is not processed by this code. |
| Taiwan Medical Care Act Art. 72 | Medical records stay with the institution; the registry stores a hash of a copy the patient obtained through their right of access. |
| Taiwan Human Subjects Research Act | A later medical pilot needs IRB review. Not started. |
| GDPR Art. 9 | Special-category data needs an explicit lawful basis; consent is recorded and can be revoked on-chain. |
| HIPAA 45 CFR 164.524 | Right of access is the intended source of the copy. This stack is not a HIPAA system and signs no BAA. |
| Singapore PDPA | Ceresus as a Singapore entity is a later contracting wrapper. Not implemented in code. |

Any medical-data pilot also has to deal with pathology-report copyright
and relatives' genetic privacy, which this skeleton does not address.

## What works, what is stubbed

**Works, with tests:**

- Retry-After circuit breaker, fallback chains, health / usage / key-status endpoints.
- x402 402 ↔ mock-pay ↔ 200 handshake for a verified result, Base Sepolia USDC quote, mainnet refused at startup.
- Useful-work runner on bundled Lean samples, mock LLM, mock Lean, stable result hash, listing the result for sale.
- `DataVaultRegistry` register / consent / tier / revoke / timed-access expiry / EIP-712 provenance, 15 Foundry tests including a fuzz on owner checks.
- Secret redaction, empty-key catalog validation (PR #1), gitignore for `.env` and Foundry `broadcast/`.

**Stubbed or explicit non-goals of this phase:**

- Live x402 facilitator, EIP-3009 verification, and any on-chain settlement. The mock never talks to RPC.
- Real Lean in CI. Documented; enabled locally with `MUSE_LEAN_MODE=real` when `lean` is on `PATH`.
- Calling formal-conjectures without a local checkout (not vendored).
- Client-side encrypted vault, hospital certificate issuance, federated learning, secure compute, cohort pooling, IRB workflow.
- Deployment of the Solidity contract (no script, no RPC, no mainnet).
- Billing of any kind (no Stripe, no invoices, no self-issued token).
- Putting patient data through the free-tier gateway.

## Running it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests -q
python scripts/validate_config.py
# Foundry (optional on a laptop; required in CI):
#   curl -L https://foundry.paradigm.xyz | bash && foundryup
cd contracts && forge install foundry-rs/forge-std --no-commit && forge test -vv

# Phase 0 service, mock LLM and mock Lean, no provider keys required:
python -m muse          # http://127.0.0.1:4100
curl -s http://127.0.0.1:4100/health

# LiteLLM proxy (unchanged from PR #1):
cp .env.example .env    # set LITELLM_MASTER_KEY and any provider keys
docker compose up -d    # http://127.0.0.1:4000
```

To have the runner ask the live proxy for proofs, set
`MUSE_WORK_LLM=litellm` and point `MUSE_BASE_URL` at the proxy. That
path is not exercised in CI and will spend quota.

## Next steps (not in this PR)

- Wire a *testnet* x402 facilitator (Base Sepolia only) behind an explicit
  flag, still refusing mainnet.
- Optional real Lean in CI via a cached elan install, or a dedicated job.
- A formal-conjectures pin and a problem-selection policy.
- Hospital allow-list, testnet deploy of the registry (deliberate,
  reviewed), and an off-chain vault client.
- Cohort pooling and the Ceresus contracting wrapper, after counsel review.
- Keep PR #1 and this PR unmerged until you say otherwise.
