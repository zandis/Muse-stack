// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessTier} from "./AccessTier.sol";

/// @title Muse registry (interface).
/// @notice On-chain state is hashes, consent, tier, a hospital provenance
///         *signature hash*, and issuer (hospital) allow-list membership.
///         The data itself never lives here. Phase 0 skeleton: no
///         deployment, no payments, no mainnet. Per-patient cohort
///         linkage on-chain is out of scope (see contracts/README.md).
interface IDataVaultRegistry {
    struct VaultView {
        address owner;
        bytes32 dataHash;
        AccessTier tier;
        address hospital;
        bytes32 hospitalSignatureHash;
        uint64 consentExpiresAt;
        uint64 accessExpiresAt;
        uint64 deadline;
        bool consentActive;
        bool revoked;
        bool attestationRevoked;
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
    event ProvenanceRecorded(
        bytes32 indexed vaultId, address indexed hospital, bytes32 dataHash, bytes32 signatureHash
    );
    event IssuerRegistered(address indexed issuer);
    event IssuerRevoked(address indexed issuer);
    event AttestationRevoked(bytes32 indexed vaultId, address indexed issuer);
    event AccessRenewed(bytes32 indexed vaultId, uint64 accessExpiresAt, bytes32 signatureHash);

    function computeVaultId(address owner, bytes32 salt) external pure returns (bytes32);

    function registerVault(
        bytes32 salt,
        bytes32 dataHash,
        AccessTier tier,
        uint64 consentExpiresAt,
        uint64 accessExpiresAt,
        address hospital,
        uint64 deadline,
        bytes calldata hospitalSignature
    ) external returns (bytes32 vaultId);

    function setConsent(bytes32 vaultId, bool active, uint64 expiresAt) external;

    function setTier(bytes32 vaultId, AccessTier tier, uint64 accessExpiresAt) external;

    function renewAccess(
        bytes32 vaultId,
        uint64 newAccessExpiresAt,
        uint64 deadline,
        bytes calldata hospitalSignature
    ) external;

    function revoke(bytes32 vaultId) external;

    function revokeAttestation(bytes32 vaultId) external;

    function registerIssuer(address issuer) external;

    function revokeIssuer(address issuer) external;

    function vault(bytes32 vaultId) external view returns (VaultView memory);

    function verifyProvenance(bytes32 vaultId) external view returns (bool);

    function isAccessible(bytes32 vaultId) external view returns (bool);

    function domainSeparator() external view returns (bytes32);
}
