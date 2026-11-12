// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessTier} from "./AccessTier.sol";

/// @title Per-person data vault registry (interface).
/// @notice On-chain state is hashes, consent, tier, and a hospital provenance
///         signature. The data itself never lives here. Phase 0 skeleton:
///         no deployment, no payments, no mainnet.
interface IDataVaultRegistry {
    struct VaultView {
        address owner;
        bytes32 dataHash;
        AccessTier tier;
        address hospital;
        bytes hospitalSignature;
        uint64 consentExpiresAt;
        uint64 accessExpiresAt;
        bool consentActive;
        bool revoked;
        uint64 registeredAt;
    }

    event VaultRegistered(
        bytes32 indexed vaultId,
        address indexed owner,
        bytes32 dataHash,
        address indexed hospital,
        AccessTier tier
    );
    event ConsentUpdated(bytes32 indexed vaultId, bool active, uint64 expiresAt);
    event TierUpdated(bytes32 indexed vaultId, AccessTier tier, uint64 accessExpiresAt);
    event Revoked(bytes32 indexed vaultId, address indexed owner);
    event ProvenanceRecorded(bytes32 indexed vaultId, address indexed hospital, bytes32 dataHash);

    function registerVault(
        bytes32 vaultId,
        bytes32 dataHash,
        AccessTier tier,
        uint64 consentExpiresAt,
        uint64 accessExpiresAt,
        address hospital,
        bytes calldata hospitalSignature
    ) external;

    function setConsent(bytes32 vaultId, bool active, uint64 expiresAt) external;

    function setTier(bytes32 vaultId, AccessTier tier, uint64 accessExpiresAt) external;

    function revoke(bytes32 vaultId) external;

    function vault(bytes32 vaultId) external view returns (VaultView memory);

    function verifyProvenance(bytes32 vaultId) external view returns (bool);

    function isAccessible(bytes32 vaultId) external view returns (bool);

    function domainSeparator() external view returns (bytes32);
}
