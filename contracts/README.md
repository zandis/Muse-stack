# Data-vault registry (phase 0 skeleton)

Solidity interfaces and a Foundry-tested implementation of the per-person
data vault described in the Muse Stack / Ceresus design.

On-chain state is **only**:

- a data hash (never the data itself)
- the patient's address (the registering `msg.sender`)
- consent (active flag + optional expiry)
- an access tier: `Query`, `Federated`, `TimedAccess`, `FullTransfer`
- a hospital EIP-712 provenance signature, recovered to the hospital address

There is **no deploy script**, no payable function, no token, and no RPC
endpoint in `foundry.toml`. Run tests only:

```bash
cd contracts
forge install foundry-rs/forge-std --no-commit   # first time, local
forge test -vv
```

Do not deploy this contract to a mainnet. A later phase will add a
deliberate testnet deploy path, a hospital allow-list, and a review.
