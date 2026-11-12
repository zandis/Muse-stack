
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

    bytes32 internal constant VAULT_ID = keccak256("vault-1");
    bytes32 internal constant DATA_HASH = keccak256("encrypted-blob-cid");

    function setUp() public {
        registry = new DataVaultRegistry();
        hospital = vm.addr(hospitalKey);
        patient = vm.addr(patientKey);
        stranger = vm.addr(strangerKey);
    }

    function _sign(bytes32 vaultId, bytes32 dataHash, address subject, address hosp, uint256 key)
        internal
        view
        returns (bytes memory)
    {
        bytes32 digest = registry.provenanceDigest(vaultId, dataHash, subject, hosp);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, digest);
        return abi.encodePacked(r, s, v);
    }

    function _register(AccessTier tier, uint64 consentExpires, uint64 accessExpires) internal {
        bytes memory sig = _sign(VAULT_ID, DATA_HASH, patient, hospital, hospitalKey);
        vm.prank(patient);
        registry.registerVault(VAULT_ID, DATA_HASH, tier, consentExpires, accessExpires, hospital, sig);
    }

    function test_registerWithValidHospitalSignature() public {
        _register(AccessTier.Query, 0, 0);
        IDataVaultRegistry.VaultView memory view_ = registry.vault(VAULT_ID);
        assertEq(view_.owner, patient);
        assertEq(view_.dataHash, DATA_HASH);
        assertEq(uint8(view_.tier), uint8(AccessTier.Query));
        assertEq(view_.hospital, hospital);
        assertTrue(view_.consentActive);
        assertFalse(view_.revoked);
        assertTrue(registry.verifyProvenance(VAULT_ID));
        assertTrue(registry.isAccessible(VAULT_ID));
    }

    function test_registerEmitsEvents() public {
        bytes memory sig = _sign(VAULT_ID, DATA_HASH, patient, hospital, hospitalKey);
        vm.expectEmit(true, true, true, true);
        emit IDataVaultRegistry.VaultRegistered(VAULT_ID, patient, DATA_HASH, hospital, AccessTier.Federated);
        vm.prank(patient);
        registry.registerVault(VAULT_ID, DATA_HASH, AccessTier.Federated, 0, 0, hospital, sig);
    }

    function test_rejectInvalidHospitalSignature() public {
        // Signed by the stranger, not the hospital.
        bytes memory sig = _sign(VAULT_ID, DATA_HASH, patient, hospital, strangerKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.InvalidHospitalSignature.selector);
        registry.registerVault(VAULT_ID, DATA_HASH, AccessTier.Query, 0, 0, hospital, sig);
    }

    function test_rejectSignatureOverWrongHash() public {
        bytes32 other = keccak256("some-other-data");
        bytes memory sig = _sign(VAULT_ID, other, patient, hospital, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.InvalidHospitalSignature.selector);
        registry.registerVault(VAULT_ID, DATA_HASH, AccessTier.Query, 0, 0, hospital, sig);
    }

    function test_rejectZeroDataHashAndZeroVaultId() public {
        bytes memory sig = _sign(VAULT_ID, bytes32(0), patient, hospital, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.ZeroHash.selector);
        registry.registerVault(VAULT_ID, bytes32(0), AccessTier.Query, 0, 0, hospital, sig);

        bytes memory sig2 = _sign(bytes32(0), DATA_HASH, patient, hospital, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(DataVaultRegistry.ZeroVaultId.selector);
        registry.registerVault(bytes32(0), DATA_HASH, AccessTier.Query, 0, 0, hospital, sig2);
    }

    function test_rejectDuplicateVaultId() public {
        _register(AccessTier.Query, 0, 0);
        bytes memory sig = _sign(VAULT_ID, DATA_HASH, patient, hospital, hospitalKey);
        vm.prank(patient);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.VaultAlreadyExists.selector, VAULT_ID));
        registry.registerVault(VAULT_ID, DATA_HASH, AccessTier.Query, 0, 0, hospital, sig);
    }

    function test_onlyOwnerCanSetConsentAndTierAndRevoke() public {
        _register(AccessTier.Query, 0, 0);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, stranger, patient));
        registry.setConsent(VAULT_ID, false, 0);

        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, stranger, patient));
        registry.setTier(VAULT_ID, AccessTier.Federated, 0);

        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, stranger, patient));
        registry.revoke(VAULT_ID);
    }

    function test_ownerRevokeIsPermanent() public {
        _register(AccessTier.Query, 0, 0);
        vm.prank(patient);
        registry.revoke(VAULT_ID);
        IDataVaultRegistry.VaultView memory view_ = registry.vault(VAULT_ID);
        assertTrue(view_.revoked);
        assertFalse(view_.consentActive);
        assertFalse(registry.isAccessible(VAULT_ID));

        vm.prank(patient);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.AlreadyRevoked.selector, VAULT_ID));
        registry.setConsent(VAULT_ID, true, 0);
    }

    function test_allFourTiers() public {
        // Four vaults, one per tier.
        AccessTier[4] memory tiers =
            [AccessTier.Query, AccessTier.Federated, AccessTier.TimedAccess, AccessTier.FullTransfer];
        for (uint256 i = 0; i < 4; i++) {
            bytes32 vaultId = keccak256(abi.encodePacked("vault", i));
            uint64 accessExpires = tiers[i] == AccessTier.TimedAccess ? uint64(block.timestamp + 30 days) : 0;
            bytes memory sig = _sign(vaultId, DATA_HASH, patient, hospital, hospitalKey);
            vm.prank(patient);
            registry.registerVault(vaultId, DATA_HASH, tiers[i], 0, accessExpires, hospital, sig);
            assertEq(uint8(registry.vault(vaultId).tier), uint8(tiers[i]));
            assertTrue(registry.isAccessible(vaultId));
        }
    }

    function test_timedAccessRequiresFutureExpiryAndExpires() public {
        vm.prank(patient);
        bytes memory sig = _sign(VAULT_ID, DATA_HASH, patient, hospital, hospitalKey);
        vm.expectRevert(DataVaultRegistry.TimedAccessRequiresExpiry.selector);
        registry.registerVault(VAULT_ID, DATA_HASH, AccessTier.TimedAccess, 0, 0, hospital, sig);

        _register(AccessTier.TimedAccess, 0, uint64(block.timestamp + 100));
        assertTrue(registry.isAccessible(VAULT_ID));
        vm.warp(block.timestamp + 101);
        assertFalse(registry.isAccessible(VAULT_ID));
    }

    function test_consentExpiryEndsAccess() public {
        _register(AccessTier.Query, uint64(block.timestamp + 50), 0);
        assertTrue(registry.isAccessible(VAULT_ID));
        vm.warp(block.timestamp + 51);
        assertFalse(registry.isAccessible(VAULT_ID));
    }

    function test_ownerCanPauseConsentWithoutRevoking() public {
        _register(AccessTier.Federated, 0, 0);
        vm.prank(patient);
        registry.setConsent(VAULT_ID, false, 0);
        assertFalse(registry.isAccessible(VAULT_ID));
        assertFalse(registry.vault(VAULT_ID).revoked);
        vm.prank(patient);
        registry.setConsent(VAULT_ID, true, 0);
        assertTrue(registry.isAccessible(VAULT_ID));
    }

    function test_unknownVaultReverts() public {
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.VaultNotFound.selector, VAULT_ID));
        registry.vault(VAULT_ID);
    }

    function test_storesHashNotData() public {
        _register(AccessTier.FullTransfer, 0, 0);
        // The only stored "data" is a 32-byte hash. There is no function that
        // accepts or returns a raw payload. This assertion guards the surface:
        IDataVaultRegistry.VaultView memory view_ = registry.vault(VAULT_ID);
        assertEq(view_.dataHash, DATA_HASH);
        assertEq(view_.dataHash.length, 32);
    }

    function testFuzz_onlyOwnerCanRevoke(address caller) public {
        _register(AccessTier.Query, 0, 0);
        vm.assume(caller != patient);
        vm.prank(caller);
        vm.expectRevert(abi.encodeWithSelector(DataVaultRegistry.NotOwner.selector, caller, patient));
        registry.revoke(VAULT_ID);
    }
}
