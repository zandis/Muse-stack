// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {DataVaultRegistry} from "../src/DataVaultRegistry.sol";
import {AccessTier} from "../src/AccessTier.sol";
import {IDataVaultRegistry} from "../src/IDataVaultRegistry.sol";

contract DataVaultRegistryTest is Test {
    DataVaultRegistry internal registry;

    uint256 internal hospitalKey = 0xA11CE;
    uint256 internal patientKey = 0xB0B;
    uint256 internal strangerKey = 0xC0FFEE;
    address internal hospital;
    address internal patient;
    address internal stranger;

    bytes32 internal constant SALT = keccak256("vault-1");
    bytes32 internal constant DATA_HASH = keccak256("encrypted-blob-cid");

    uint64 internal deadline;

    function setUp() public {
        // Start at a non-zero, comfortable timestamp so "past" values
        // used in expiry tests are not 0 (0 means "no expiry").
        vm.warp(1_700_000_000);
        registry = new DataVaultRegistry();
        hospital = vm.addr(hospitalKey);
        patient = vm.addr(patientKey);
        stranger = vm.addr(strangerKey);
        deadline = uint64(block.timestamp + 1 days);
        // Hospital must be on the admin issuer allow-list.
        registry.registerIssuer(hospital);
    }

    function _vaultId() internal view returns (bytes32) {
        return registry.computeVaultId(patient, SALT);
    }

    function _sign(
        bytes32 vaultId,
        bytes32 dataHash,
        address subject,
        address hosp,
        uint64 dl,
        uint256 key
    ) internal view returns (bytes memory) {
        bytes32 digest = registry.provenanceDigest(vaultId, dataHash, subject, hosp, dl);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    function _signRenewal(bytes32 vaultId, uint64 accessExpires, uint64 dl, uint256 key)
        internal
        view
        returns (bytes memory)
    {
        bytes32 digest =
            registry.renewalDigest(vaultId, DATA_HASH, patient, hospital, accessExpires, dl);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    function _register(AccessTier tier, uint64 consentExpires, uint64 accessExpires)
        internal
        returns (bytes32 vaultId)
    {
        vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        bytes32 returned = registry.registerVault(
            SALT, DATA_HASH, tier, consentExpires, accessExpires, hospital, deadline, sig
        );
        assertEq(returned, vaultId);
    }

    function test_adminIsDeployer() public view {
        assertEq(registry.admin(), address(this));
        assertTrue(registry.isIssuer(hospital));
    }

    function test_registerWithValidHospitalSignature() public {
        bytes32 vaultId = _register(AccessTier.Query, 0, 0);
        IDataVaultRegistry.VaultView memory view_ = registry.vault(vaultId);
        assertEq(view_.owner, patient);
        assertEq(view_.dataHash, DATA_HASH);
        assertEq(uint8(view_.tier), uint8(AccessTier.Query));
        assertEq(view_.hospital, hospital);
        assertTrue(view_.consentActive);
        assertFalse(view_.revoked);
        assertFalse(view_.attestationRevoked);
        assertTrue(registry.verifyProvenance(vaultId));
        assertTrue(registry.isAccessible(vaultId));
        // The raw 65-byte signature is not stored — only its hash.
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        assertEq(view_.hospitalSignatureHash, keccak256(sig));
        assertEq(view_.hospitalSignatureHash.length, 32);
    }

    function test_registerEmitsEvents() public {
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.expectEmit(true, true, true, true);
        emit IDataVaultRegistry.VaultRegistered(vaultId, patient, DATA_HASH, hospital, AccessTier.Federated);
        vm.prank(patient);
        registry.registerVault(SALT, DATA_HASH, AccessTier.Federated, 0, 0, hospital, deadline, sig);
    }

    function test_rejectInvalidHospitalSignature() public {
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, strangerKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.InvalidHospitalSignature.selector);
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, deadline, sig);
    }

    function test_rejectSignatureOverWrongHash() public {
        bytes32 other = keccak256("some-other-data");
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, other, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.InvalidHospitalSignature.selector);
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, deadline, sig);
    }

    function test_rejectExpiredDeadline() public {
        uint64 past = uint64(block.timestamp - 1);
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, past, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.DeadlineExpired.selector);
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, past, sig);
    }

    function test_rejectPastConsentExpiry() public {
        uint64 past = uint64(block.timestamp - 1);
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.ConsentAlreadyExpired.selector);
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, past, 0, hospital, deadline, sig);
    }

    function test_rejectUnregisteredIssuer() public {
        address otherHospital = vm.addr(0xD00D);
        bytes32 vaultId = registry.computeVaultId(patient, SALT);
        // Sign as if this hospital were valid, but it is not on the list.
        // Use a signature from a key we control — even a valid-looking
        // recover would still fail the issuer check first.
        uint64 dl = deadline;
        bytes32 digest = registry.provenanceDigest(vaultId, DATA_HASH, patient, otherHospital, dl);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(0xD00D, digest);
        bytes memory sig = abi.encodePacked(r, s, v);
        vm.prank(patient);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.IssuerNotRegistered.selector, otherHospital));
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, otherHospital, dl, sig);
    }

    function test_onlyAdminCanRegisterAndRevokeIssuer() public {
        address other = vm.addr(0x1111);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotAdmin.selector, stranger));
        registry.registerIssuer(other);

        registry.registerIssuer(other);
        assertTrue(registry.isIssuer(other));

        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotAdmin.selector, stranger));
        registry.revokeIssuer(other);

        registry.revokeIssuer(other);
        assertFalse(registry.isIssuer(other));
    }

    function test_revokeIssuerBlocksNewRegistrations() public {
        registry.revokeIssuer(hospital);
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.IssuerNotRegistered.selector, hospital));
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, deadline, sig);
    }

    function test_vaultIdIsBoundToOwnerSaltCannotBeSquatted() public {
        // The stranger uses the same salt. The resulting id is theirs, not
        // the patient's, so they cannot occupy the patient's slot.
        bytes32 patientId = registry.computeVaultId(patient, SALT);
        bytes32 strangerId = registry.computeVaultId(stranger, SALT);
        assertTrue(patientId != strangerId);

        bytes memory sig = _sign(strangerId, DATA_HASH, stranger, hospital, deadline, hospitalKey);
        vm.prank(stranger);
        bytes32 got = registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, deadline, sig);
        assertEq(got, strangerId);

        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.VaultNotFound.selector, patientId));
        registry.vault(patientId);

        // The patient can still register their own id with the same salt.
        bytes memory sigP = _sign(patientId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        assertEq(
            registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, deadline, sigP),
            patientId
        );
    }

    function test_rejectZeroDataHash() public {
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, bytes32(0), patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.ZeroHash.selector);
        registry.registerVault(SALT, bytes32(0), AccessTier.Query, 0, 0, hospital, deadline, sig);
    }

    function test_rejectDuplicateVaultId() public {
        bytes32 vaultId = _register(AccessTier.Query, 0, 0);
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.VaultAlreadyExists.selector, vaultId));
        registry.registerVault(SALT, DATA_HASH, AccessTier.Query, 0, 0, hospital, deadline, sig);
    }

    function test_onlyOwnerCanSetConsentAndTierAndRevoke() public {
        bytes32 vaultId = _register(AccessTier.Query, 0, 0);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, stranger, patient));
        registry.setConsent(vaultId, false, 0);

        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, stranger, patient));
        registry.setTier(vaultId, AccessTier.Federated, 0);

        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, stranger, patient));
        registry.revoke(vaultId);
    }

    function test_ownerRevokeIsPermanent() public {
        bytes32 vaultId = _register(AccessTier.Query, 0, 0);
        vm.prank(patient);
        registry.revoke(vaultId);
        IDataVaultRegistry.VaultView memory view_ = registry.vault(vaultId);
        assertTrue(view_.revoked);
        assertFalse(view_.consentActive);
        assertFalse(registry.isAccessible(vaultId));

        vm.prank(patient);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.AlreadyRevoked.selector, vaultId));
        registry.setConsent(vaultId, true, 0);
    }

    function test_hospitalCanRevokeAttestation() public {
        bytes32 vaultId = _register(AccessTier.Query, 0, 0);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotIssuerOrAdmin.selector, stranger));
        registry.revokeAttestation(vaultId);

        vm.prank(hospital);
        registry.revokeAttestation(vaultId);
        assertTrue(registry.vault(vaultId).attestationRevoked);
        assertFalse(registry.isAccessible(vaultId));
        assertFalse(registry.verifyProvenance(vaultId));
    }

    function test_allFourTiers() public {
        AccessTier[4] memory tiers =
            [AccessTier.Query, AccessTier.Federated, AccessTier.TimedAccess, AccessTier.FullTransfer];
        for (uint256 i = 0; i < 4; i++) {
            bytes32 salt = keccak256(abi.encodePacked("vault", i));
            bytes32 vaultId = registry.computeVaultId(patient, salt);
            uint64 accessExpires = tiers[i] == AccessTier.TimedAccess ? uint64(block.timestamp + 30 days) : 0;
            bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
            vm.prank(patient);
            registry.registerVault(salt, DATA_HASH, tiers[i], 0, accessExpires, hospital, deadline, sig);
            assertEq(uint8(registry.vault(vaultId).tier), uint8(tiers[i]));
            assertTrue(registry.isAccessible(vaultId));
        }
    }

    function test_timedAccessRequiresFutureExpiryAndExpires() public {
        bytes32 vaultId = _vaultId();
        bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, deadline, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.TimedAccessRequiresExpiry.selector);
        registry.registerVault(SALT, DATA_HASH, AccessTier.TimedAccess, 0, 0, hospital, deadline, sig);

        _register(AccessTier.TimedAccess, 0, uint64(block.timestamp + 100));
        assertTrue(registry.isAccessible(vaultId));
        vm.warp(block.timestamp + 101);
        assertFalse(registry.isAccessible(vaultId));
    }

    function test_accessExpiresAtAppliesToEveryTier() public {
        // A Query vault with a finite accessExpiresAt must lose access
        // when that window ends — not only TimedAccess.
        bytes32 vaultId = _register(AccessTier.Query, 0, uint64(block.timestamp + 50));
        assertTrue(registry.isAccessible(vaultId));
        vm.warp(block.timestamp + 51);
        assertFalse(registry.isAccessible(vaultId));
    }

    function test_setTierCannotClearExtendOrReviveExpiredAccess() public {
        uint64 original = uint64(block.timestamp + 100);
        bytes32 vaultId = _register(AccessTier.TimedAccess, 0, original);

        // Cannot clear.
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.AccessExpiryImmutable.selector);
        registry.setTier(vaultId, AccessTier.Query, 0);

        // Cannot extend.
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.AccessExpiryImmutable.selector);
        registry.setTier(vaultId, AccessTier.Query, original + 1000);

        // Changing to Query with the same (or shorter) window is allowed
        // *before* expiry — but does not extend access.
        vm.prank(patient);
        registry.setTier(vaultId, AccessTier.Query, original);
        assertEq(uint8(registry.vault(vaultId).tier), uint8(AccessTier.Query));
        assertEq(registry.vault(vaultId).accessExpiresAt, original);

        // After expiry, setTier to Query cannot revive access.
        vm.warp(original + 1);
        assertFalse(registry.isAccessible(vaultId));
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.ExpiredAccessRequiresNewSignature.selector);
        registry.setTier(vaultId, AccessTier.Query, original);
    }

    function test_renewAccessWithNewHospitalSignature() public {
        uint64 original = uint64(block.timestamp + 50);
        bytes32 vaultId = _register(AccessTier.TimedAccess, 0, original);
        vm.warp(original + 1);
        assertFalse(registry.isAccessible(vaultId));

        uint64 fresh = uint64(block.timestamp + 200);
        uint64 dl = uint64(block.timestamp + 1 days);
        bytes memory sig = _signRenewal(vaultId, fresh, dl, hospitalKey);
        vm.prank(patient);
        registry.renewAccess(vaultId, fresh, dl, sig);
        assertTrue(registry.isAccessible(vaultId));
        assertEq(registry.vault(vaultId).accessExpiresAt, fresh);
    }

    function test_consentExpiryEndsAccess() public {
        bytes32 vaultId = _register(AccessTier.Query, uint64(block.timestamp + 50), 0);
        assertTrue(registry.isAccessible(vaultId));
        vm.warp(block.timestamp + 51);
        assertFalse(registry.isAccessible(vaultId));
    }

    function test_ownerCanPauseConsentWithoutRevoking() public {
        bytes32 vaultId = _register(AccessTier.Federated, 0, 0);
        vm.prank(patient);
        registry.setConsent(vaultId, false, 0);
        assertFalse(registry.isAccessible(vaultId));
        assertFalse(registry.vault(vaultId).revoked);
        vm.prank(patient);
        registry.setConsent(vaultId, true, 0);
        assertTrue(registry.isAccessible(vaultId));
    }

    function test_unknownVaultReverts() public {
        // Cache the id: passing `_vaultId()` (a call to the registry)
        // as an argument after `expectRevert` would be the "next call".
        bytes32 vaultId = _vaultId();
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.VaultNotFound.selector, vaultId));
        registry.vault(vaultId);
    }

    function test_storesHashNotData() public {
        bytes32 vaultId = _register(AccessTier.FullTransfer, 0, 0);
        IDataVaultRegistry.VaultView memory view_ = registry.vault(vaultId);
        assertEq(view_.dataHash, DATA_HASH);
        assertEq(view_.dataHash.length, 32);
    }

    function testFuzz_onlyOwnerCanRevoke(address caller) public {
        bytes32 vaultId = _register(AccessTier.Query, 0, 0);
        vm.assume(caller != patient);
        vm.prank(caller);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, caller, patient));
        registry.revoke(vaultId);
    }

    function testFuzz_vaultIdIsOwnerBound(address a, address b, bytes32 salt) public view {
        vm.assume(a != b);
        assertTrue(registry.computeVaultId(a, salt) != registry.computeVaultId(b, salt));
    }
}
