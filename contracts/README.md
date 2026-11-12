# Muse registry (phase 0 skeleton)

Solidity interfaces and a Foundry-tested implementation of the
**registry** described in the Muse Stack / Ceresus design. This is a
registry of hashes and consent, not a "data vault": the data itself
never lives here.

On-chain state is **only**:

- a data hash (never the data itself)
- the patient's address (the registering `msg.sender`)
- a vault id equal to `keccak256(abi.encode(owner, salt))` — a third
  party cannot squat on another patient's id
- consent (active flag + optional expiry; a past `consentExpiresAt` is
  rejected at registration unless it is 0)
- an access tier: `Query`, `Federated`, `TimedAccess`, `FullTransfer`
- `accessExpiresAt`, which applies to *every* tier whenever it is
  non-zero. `setTier` cannot clear it, extend it, or revive an expired
  window (a new hospital signature via `renewAccess` is required)
- a hospital address that must be on the admin's issuer allow-list
- the *hash* of a hospital EIP-712 provenance signature (the 65-byte
  signature is not stored; it is emitted as a hash in an event)
- an EIP-712 `deadline` on the signed attestation
- attestation revocation (hospital or admin) and issuer revocation
  (admin only). The admin is the deployer.

The EIP-712 domain name is **`Muse Registry`**.

There is **no deploy script**, no payable function, no token, and no RPC
endpoint in `foundry.toml`. Run tests only:

```bash
cd contracts
forge install foundry-rs/forge-std@v1.17.0 --no-commit   # first time, local
forge test -vv
```

CI pins `foundry-rs/foundry-toolchain@v1` at Foundry `1.9.0` and
installs `forge-std` at `v1.17.0` (see `foundry.lock`).

Do not deploy this contract to a mainnet. A later phase will add a
deliberate testnet deploy path, after review.

## Out of scope: on-chain cohort linkage

This skeleton is **not** a privacy-preserving data market. A later
design that wants to support research cohorts must keep per-patient
linkage *off* the chain: no on-chain membership lists, no join of
patient identifiers with cohort identifiers, no public enumeration of
who is in which study. That work is research, pending a privacy
review, and is deliberately not started here. The contract records a
hash and consent for one registering address; it must not be extended
into a cohort registry without that review.
