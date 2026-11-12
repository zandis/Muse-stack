// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessTier} from "./AccessTier.sol";
import {IDataVaultRegistry} from "./IDataVaultRegistry.sol";

/// @title Muse registry (phase 0 skeleton).
/// @notice A patient (`msg.sender`) registers a hash of their data, a
///         consent window, an access tier, and a hospital EIP-712
///         signature that binds the hospital (an allow-listed issuer)
///         to that hash. The vault id is `keccak256(abi.encode(owner, salt))`
///         so a third party cannot squat on another patient's id.
///
///         The 65-byte signature is *not* stored: only `keccak256(signature)`
///         and an event. The EIP-712 type includes a `deadline`. After
///         registration, `accessExpiresAt` applies to every tier whenever
///         it is non-zero. `setTier` cannot clear, extend, or revive an
///         expired window; a new hospital signature (`renewAccess`) is
///         required to extend. Hospitals (or the admin) can revoke an
///         attestation; the admin can revoke an issuer.
///
///         This is a registry of hashes and consent, not a "data vault".
///         Do not deploy it to a mainnet. There is no payable function
///         and no token transfer. Per-patient cohort linkage on-chain is
///         out of scope pending a privacy design (see contracts/README.md).
contract DataVaultRegistry is IDataVaultRegistry {
    error VaultAlreadyExists(bytes32 vaultId);
    error VaultNotFound(bytes32 vaultId);
    error NotOwner(address caller, address owner);
    error NotAdmin(address caller);
    error InvalidHospitalSignature();
    error ZeroHash();
    error ZeroHospital();
    error ZeroIssuer();
    error AlreadyRevoked(bytes32 vaultId);
    error TimedAccessRequiresExpiry();
    error IssuerNotRegistered(address issuer);
    error IssuerAlreadyRegistered(address issuer);
    error IssuerNotOnList(address issuer);
    error DeadlineExpired();
    error ConsentAlreadyExpired();
    error AccessAlreadyExpired();
    error AccessExpiryImmutable();
    error ExpiredAccessRequiresNewSignature();
    error AttestationAlreadyRevoked(bytes32 vaultId);
    error NotIssuerOrAdmin(address caller);

    bytes32 private constant _EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");

    // Includes a deadline so a signed attestation cannot be held and
    // replayed indefinitely.
    bytes32 private constant _PROVENANCE_TYPEHASH = keccak256(
        "HospitalProvenance(bytes32 vaultId,bytes32 dataHash,address subject,address hospital,uint64 deadline)"
    );

    bytes32 private constant _RENEWAL_TYPEHASH = keccak256(
        "HospitalRenewal(bytes32 vaultId,bytes32 dataHash,address subject,address hospital,uint64 accessExpiresAt,uint64 deadline)"
    );

    // EIP-712 name is "Muse Registry" (not "Muse DataVault").
    bytes32 private constant _NAME_HASH = keccak256("Muse Registry");
    bytes32 private constant _VERSION_HASH = keccak256("1");

    struct Vault {
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

    mapping(bytes32 => Vault) private _vaults;
    mapping(address => bool) public isIssuer;

    address public immutable admin;

    constructor() {
        admin = msg.sender;
    }

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin(msg.sender);
        _;
    }

    /// @inheritdoc IDataVaultRegistry
    function computeVaultId(address owner, bytes32 salt) public pure returns (bytes32) {
        return keccak256(abi.encode(owner, salt));
    }

    /// @inheritdoc IDataVaultRegistry
    function registerIssuer(address issuer) external onlyAdmin {
        if (issuer == address(0)) revert ZeroIssuer();
        if (isIssuer[issuer]) revert IssuerAlreadyRegistered(issuer);
        isIssuer[issuer] = true;
        emit IssuerRegistered(issuer);
    }

    /// @inheritdoc IDataVaultRegistry
    function revokeIssuer(address issuer) external onlyAdmin {
        if (!isIssuer[issuer]) revert IssuerNotOnList(issuer);
        isIssuer[issuer] = false;
        emit IssuerRevoked(issuer);
    }

    /// @inheritdoc IDataVaultRegistry
    function registerVault(
        bytes32 salt,
        bytes32 dataHash,
        AccessTier tier,
        uint64 consentExpiresAt,
        uint64 accessExpiresAt,
        address hospital,
        uint64 deadline,
        bytes calldata hospitalSignature
    ) external returns (bytes32 vaultId) {
        if (dataHash == bytes32(0)) revert ZeroHash();
        if (hospital == address(0)) revert ZeroHospital();
        if (!isIssuer[hospital]) revert IssuerNotRegistered(hospital);
        if (deadline < block.timestamp) revert DeadlineExpired();
        if (consentExpiresAt != 0 && consentExpiresAt < block.timestamp) {
            revert ConsentAlreadyExpired();
        }
        if (accessExpiresAt != 0 && accessExpiresAt <= block.timestamp) {
            revert AccessAlreadyExpired();
        }
        _requireTimedExpiry(tier, accessExpiresAt);

        address subject = msg.sender;
        vaultId = computeVaultId(subject, salt);
        if (_vaults[vaultId].owner != address(0)) revert VaultAlreadyExists(vaultId);

        if (!_signatureMatches(vaultId, dataHash, subject, hospital, deadline, hospitalSignature)) {
            revert InvalidHospitalSignature();
        }

        bytes32 sigHash = keccak256(hospitalSignature);

        Vault storage item = _vaults[vaultId];
        item.owner = subject;
        item.dataHash = dataHash;
        item.tier = tier;
        item.hospital = hospital;
        item.hospitalSignatureHash = sigHash;
        item.consentExpiresAt = consentExpiresAt;
        item.accessExpiresAt = accessExpiresAt;
        item.deadline = deadline;
        item.consentActive = true;
        item.revoked = false;
        item.attestationRevoked = false;
        item.registeredAt = uint64(block.timestamp);

        emit VaultRegistered(vaultId, subject, dataHash, hospital, tier);
        emit ProvenanceRecorded(vaultId, hospital, dataHash, sigHash);
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
        if (item.attestationRevoked) revert AttestationAlreadyRevoked(vaultId);
        // Cannot revive an already-expired access window by changing tier
        // (e.g. TimedAccess → Query) — a new hospital signature is required.
        if (item.accessExpiresAt != 0 && block.timestamp > item.accessExpiresAt) {
            revert ExpiredAccessRequiresNewSignature();
        }
        // Cannot clear a finite window (set it to 0) or extend it.
        if (item.accessExpiresAt != 0) {
            if (accessExpiresAt == 0 || accessExpiresAt > item.accessExpiresAt) {
                revert AccessExpiryImmutable();
            }
        }
        uint64 effective = accessExpiresAt == 0 ? item.accessExpiresAt : accessExpiresAt;
        _requireTimedExpiry(tier, effective);
        item.tier = tier;
        if (accessExpiresAt != 0) {
            item.accessExpiresAt = accessExpiresAt;
        }
        emit TierUpdated(vaultId, tier, item.accessExpiresAt);
    }

    /// @inheritdoc IDataVaultRegistry
    function renewAccess(
        bytes32 vaultId,
        uint64 newAccessExpiresAt,
        uint64 deadline,
        bytes calldata hospitalSignature
    ) external {
        Vault storage item = _requireOwner(vaultId);
        if (item.revoked) revert AlreadyRevoked(vaultId);
        if (item.attestationRevoked) revert AttestationAlreadyRevoked(vaultId);
        if (deadline < block.timestamp) revert DeadlineExpired();
        if (newAccessExpiresAt == 0 || newAccessExpiresAt <= block.timestamp) {
            revert AccessAlreadyExpired();
        }
        if (!isIssuer[item.hospital]) revert IssuerNotRegistered(item.hospital);
        bytes32 digest = renewalDigest(
            vaultId, item.dataHash, item.owner, item.hospital, newAccessExpiresAt, deadline
        );
        if (!_recoverMatches(digest, hospitalSignature, item.hospital)) {
            revert InvalidHospitalSignature();
        }
        bytes32 sigHash = keccak256(hospitalSignature);
        item.accessExpiresAt = newAccessExpiresAt;
        item.deadline = deadline;
        item.hospitalSignatureHash = sigHash;
        emit AccessRenewed(vaultId, newAccessExpiresAt, sigHash);
        emit ProvenanceRecorded(vaultId, item.hospital, item.dataHash, sigHash);
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
    function revokeAttestation(bytes32 vaultId) external {
        Vault storage item = _existing(vaultId);
        if (item.attestationRevoked) revert AttestationAlreadyRevoked(vaultId);
        if (msg.sender != item.hospital && msg.sender != admin) {
            revert NotIssuerOrAdmin(msg.sender);
        }
        item.attestationRevoked = true;
        emit AttestationRevoked(vaultId, item.hospital);
    }

    /// @inheritdoc IDataVaultRegistry
    function vault(bytes32 vaultId) external view returns (VaultView memory) {
        Vault storage item = _existing(vaultId);
        return VaultView({
            owner: item.owner,
            dataHash: item.dataHash,
            tier: item.tier,
            hospital: item.hospital,
            hospitalSignatureHash: item.hospitalSignatureHash,
            consentExpiresAt: item.consentExpiresAt,
            accessExpiresAt: item.accessExpiresAt,
            deadline: item.deadline,
            consentActive: item.consentActive,
            revoked: item.revoked,
            attestationRevoked: item.attestationRevoked,
            registeredAt: item.registeredAt
        });
    }

    /// @inheritdoc IDataVaultRegistry
    function verifyProvenance(bytes32 vaultId) external view returns (bool) {
        Vault storage item = _existing(vaultId);
        // The raw 65-byte signature is not stored (only its hash). A
        // successful registration already recovered the hospital; this
        // view reports that the attestation is still in force.
        return item.hospitalSignatureHash != bytes32(0) && !item.attestationRevoked
            && item.hospital != address(0);
    }

    /// @inheritdoc IDataVaultRegistry
    function isAccessible(bytes32 vaultId) public view returns (bool) {
        Vault storage item = _existing(vaultId);
        if (item.revoked || item.attestationRevoked || !item.consentActive) return false;
        if (item.consentExpiresAt != 0 && block.timestamp > item.consentExpiresAt) return false;
        // accessExpiresAt applies to *every* tier whenever it is non-zero.
        if (item.accessExpiresAt != 0 && block.timestamp > item.accessExpiresAt) return false;
        return true;
    }

    /// @inheritdoc IDataVaultRegistry
    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(_EIP712_DOMAIN_TYPEHASH, _NAME_HASH, _VERSION_HASH, block.chainid, address(this))
        );
    }

    /// @notice EIP-712 digest a hospital must sign for a given vault registration.
    function provenanceDigest(
        bytes32 vaultId,
        bytes32 dataHash,
        address subject,
        address hospital,
        uint64 deadline
    ) public view returns (bytes32) {
        bytes32 structHash =
            keccak256(abi.encode(_PROVENANCE_TYPEHASH, vaultId, dataHash, subject, hospital, deadline));
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash));
    }

    /// @notice EIP-712 digest a hospital must sign to extend an access window.
    function renewalDigest(
        bytes32 vaultId,
        bytes32 dataHash,
        address subject,
        address hospital,
        uint64 accessExpiresAt,
        uint64 deadline
    ) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(_RENEWAL_TYPEHASH, vaultId, dataHash, subject, hospital, accessExpiresAt, deadline)
        );
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
        uint64 deadline,
        bytes memory signature
    ) private view returns (bool) {
        bytes32 digest = provenanceDigest(vaultId, dataHash, subject, hospital, deadline);
        return _recoverMatches(digest, signature, hospital);
    }

    function _recoverMatches(bytes32 digest, bytes memory signature, address hospital)
        private
        pure
        returns (bool)
    {
        if (signature.length != 65) return false;
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
