
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessTier} from "./AccessTier.sol";
import {IDataVaultRegistry} from "./IDataVaultRegistry.sol";

/// @title Per-person data vault registry.
/// @notice A patient (`msg.sender`) registers a hash of their data, a consent
///         window, an access tier, and a hospital EIP-712 signature that binds
///         the hospital address to that hash. Ownership is the registering
///         address. The owner can change consent, change tier, or revoke.
///
///         This is a phase 0 skeleton. It is covered by Foundry tests only.
///         Do not deploy it to a mainnet. There is no payable function and
///         no token transfer.
contract DataVaultRegistry is IDataVaultRegistry {
    error VaultAlreadyExists(bytes32 vaultId);
    error VaultNotFound(bytes32 vaultId);
    error NotOwner(address caller, address owner);
    error InvalidHospitalSignature();
    error ZeroHash();
    error ZeroVaultId();
    error ZeroHospital();
    error AlreadyRevoked(bytes32 vaultId);
    error TimedAccessRequiresExpiry();

    bytes32 private constant _EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");

    bytes32 private constant _PROVENANCE_TYPEHASH =
        keccak256("HospitalProvenance(bytes32 vaultId,bytes32 dataHash,address subject,address hospital)");

    bytes32 private constant _NAME_HASH = keccak256("Muse DataVault");
    bytes32 private constant _VERSION_HASH = keccak256("1");

    struct Vault {
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

    mapping(bytes32 => Vault) private _vaults;

    /// @inheritdoc IDataVaultRegistry
    function registerVault(
        bytes32 vaultId,
        bytes32 dataHash,
        AccessTier tier,
        uint64 consentExpiresAt,
        uint64 accessExpiresAt,
        address hospital,
        bytes calldata hospitalSignature
    ) external {
        if (vaultId == bytes32(0)) revert ZeroVaultId();
        if (dataHash == bytes32(0)) revert ZeroHash();
        if (hospital == address(0)) revert ZeroHospital();
        if (_vaults[vaultId].owner != address(0)) revert VaultAlreadyExists(vaultId);
        _requireTimedExpiry(tier, accessExpiresAt);

        address subject = msg.sender;
        if (!_signatureMatches(vaultId, dataHash, subject, hospital, hospitalSignature)) {
            revert InvalidHospitalSignature();
        }

        Vault storage item = _vaults[vaultId];
        item.owner = subject;
        item.dataHash = dataHash;
        item.tier = tier;
        item.hospital = hospital;
        item.hospitalSignature = hospitalSignature;
        item.consentExpiresAt = consentExpiresAt;
        item.accessExpiresAt = accessExpiresAt;
        item.consentActive = true;
        item.revoked = false;
        item.registeredAt = uint64(block.timestamp);

        emit VaultRegistered(vaultId, subject, dataHash, hospital, tier);
        emit ProvenanceRecorded(vaultId, hospital, dataHash);
        emit ConsentUpdated(vaultId, true, consentExpiresAt);
    }

    /// @inheritdoc IDataVaultRegistry
    function setConsent(bytes32 vaultId, bool active, uint64 expiresAt) external {
        Vault storage item = _requireOwner(vaultId);
        if (item.revoked) revert AlreadyRevoked(vaultId);
        item.consentActive = active;
        item.consentExpiresAt = expiresAt;
        emit ConsentUpdated(vaultId, active, expiresAt);
    }

    /// @inheritdoc IDataVaultRegistry
    function setTier(bytes32 vaultId, AccessTier tier, uint64 accessExpiresAt) external {
        Vault storage item = _requireOwner(vaultId);
        if (item.revoked) revert AlreadyRevoked(vaultId);
        _requireTimedExpiry(tier, accessExpiresAt);
        item.tier = tier;
        item.accessExpiresAt = accessExpiresAt;
        emit TierUpdated(vaultId, tier, accessExpiresAt);
    }

    /// @inheritdoc IDataVaultRegistry
    function revoke(bytes32 vaultId) external {
        Vault storage item = _requireOwner(vaultId);
        if (item.revoked) revert AlreadyRevoked(vaultId);
        item.revoked = true;
        item.consentActive = false;
        emit Revoked(vaultId, item.owner);
        emit ConsentUpdated(vaultId, false, item.consentExpiresAt);
    }

    /// @inheritdoc IDataVaultRegistry
    function vault(bytes32 vaultId) external view returns (VaultView memory) {
        Vault storage item = _existing(vaultId);
        return VaultView({
            owner: item.owner,
            dataHash: item.dataHash,
            tier: item.tier,
            hospital: item.hospital,
            hospitalSignature: item.hospitalSignature,
            consentExpiresAt: item.consentExpiresAt,
            accessExpiresAt: item.accessExpiresAt,
            consentActive: item.consentActive,
            revoked: item.revoked,
            registeredAt: item.registeredAt
        });
    }

    /// @inheritdoc IDataVaultRegistry
    function verifyProvenance(bytes32 vaultId) external view returns (bool) {
        Vault storage item = _existing(vaultId);
        return _signatureMatches(vaultId, item.dataHash, item.owner, item.hospital, item.hospitalSignature);
    }

    /// @inheritdoc IDataVaultRegistry
    function isAccessible(bytes32 vaultId) public view returns (bool) {
        Vault storage item = _existing(vaultId);
        if (item.revoked || !item.consentActive) return false;
        if (item.consentExpiresAt != 0 && block.timestamp > item.consentExpiresAt) return false;
        if (item.tier == AccessTier.TimedAccess) {
            if (item.accessExpiresAt == 0 || block.timestamp > item.accessExpiresAt) return false;
        }
        return true;
    }

    /// @inheritdoc IDataVaultRegistry
    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(_EIP712_DOMAIN_TYPEHASH, _NAME_HASH, _VERSION_HASH, block.chainid, address(this))
        );
    }

    /// @notice EIP-712 digest a hospital must sign for a given vault.
    function provenanceDigest(bytes32 vaultId, bytes32 dataHash, address subject, address hospital)
        public
        view
        returns (bytes32)
    {
        bytes32 structHash = keccak256(abi.encode(_PROVENANCE_TYPEHASH, vaultId, dataHash, subject, hospital));
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash));
    }

    function _requireOwner(bytes32 vaultId) private view returns (Vault storage item) {
        item = _existing(vaultId);
        if (msg.sender != item.owner) revert NotOwner(msg.sender, item.owner);
    }

    function _existing(bytes32 vaultId) private view returns (Vault storage item) {
        item = _vaults[vaultId];
        if (item.owner == address(0)) revert VaultNotFound(vaultId);
    }

    function _requireTimedExpiry(AccessTier tier, uint64 accessExpiresAt) private view {
        if (tier == AccessTier.TimedAccess) {
            if (accessExpiresAt == 0 || accessExpiresAt <= block.timestamp) {
                revert TimedAccessRequiresExpiry();
            }
        }
    }

    function _signatureMatches(
        bytes32 vaultId,
        bytes32 dataHash,
        address subject,
        address hospital,
        bytes memory signature
    ) private view returns (bool) {
        if (signature.length != 65) return false;
        bytes32 digest = provenanceDigest(vaultId, dataHash, subject, hospital);
        address recovered = _recover(digest, signature);
        return recovered != address(0) && recovered == hospital;
    }

    function _recover(bytes32 digest, bytes memory signature) private pure returns (address) {
        bytes32 r;
        bytes32 s;
        uint8 v;
        // signature is a memory bytes of length 65: r (32) || s (32) || v (1)
        assembly ("memory-safe") {
            r := mload(add(signature, 0x20))
            s := mload(add(signature, 0x40))
            v := byte(0, mload(add(signature, 0x60)))
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) return address(0);
        // Reject short-s signatures at the malleability bound (secp256k1).
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return address(0);
        }
        return ecrecover(digest, v, r, s);
    }
}
