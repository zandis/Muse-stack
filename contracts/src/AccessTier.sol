// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Access tiers for a per-person data vault.
/// @notice Matches the Muse Stack Ceresus design (query → federated →
///         timed access → full transfer). Higher tiers mean higher privacy
///         risk and, off-chain, a higher price. The contract does not price
///         or move funds; it only records the agreed tier.
enum AccessTier {
    Query,
    Federated,
    TimedAccess,
    FullTransfer
}
