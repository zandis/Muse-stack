// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Access tiers recorded by the Muse registry.
/// @notice Matches the Muse Stack Ceresus design (query → federated →
///         timed access → full transfer). Higher tiers mean higher privacy
///         risk and, off-chain, a higher price. The contract does not price
///         or move funds; it only records the agreed tier.
///
///         This is a *registry* of hashes and consent, not a data vault:
///         the data itself is never stored here, and a later privacy design
///         must not put per-patient cohort linkage on-chain.
enum AccessTier {
    Query,
    Federated,
    TimedAccess,
    FullTransfer
}
