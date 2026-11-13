//! The vault through its public interface: round trips, every way of failing
//! closed, rotation, the lost-device flow, deletion, export and import.
//!
//! Randomised tests use a fixed seed (`TestRng`), so a failure is repeatable.

use muse_core::vault::header::{KIND_DEVICE, KIND_PRF, KIND_RECOVERY};
use muse_core::vault::primitives::{bucket_size, head_bucket, normalise_index_value};
use muse_core::vault::{
    Argon2Params, DeviceKeyStore, Header, Language, MemoryKeyStore, MemoryStore, MnemonicError, PassphraseKeyStore,
    RecoveryKey, RecoveryPolicy, Secret32, StoredRecord, TestRng, Vault, VaultError, VaultRng, VaultStore,
};
use unicode_normalization::UnicodeNormalization;
use zeroize::Zeroize;

const NOW: u64 = 1_800_000_000;

struct Fixture {
    vault: Vault<MemoryStore>,
    recovery: RecoveryKey,
    keystore: MemoryKeyStore,
    rng: TestRng,
}

fn fixture(seed: &str) -> Fixture {
    let mut rng = TestRng::new(seed.as_bytes());
    let keystore = MemoryKeyStore::new(&mut rng).unwrap();
    let (vault, recovery) = Vault::create(MemoryStore::new(), "laptop", &keystore, &mut rng, NOW).unwrap();
    Fixture { vault, recovery, keystore, rng }
}

/// Confirms the recovery words (needed before a passkey slot may be added).
fn confirm(f: &mut Fixture) {
    let words: Vec<String> = [3usize, 11, 24].iter().map(|p| f.recovery.word(Language::English, *p).unwrap().to_string()).collect();
    let entries = [(3u8, words[0].as_str()), (11, words[1].as_str()), (24, words[2].as_str())];
    assert!(f.vault.verify_recovery_words(Language::English, &entries, NOW + 1).unwrap());
}

fn other_keystore(seed: &str) -> MemoryKeyStore {
    MemoryKeyStore::new(&mut TestRng::new(seed.as_bytes())).unwrap()
}

fn bytes(rng: &mut TestRng, n: usize) -> Vec<u8> {
    let mut out = vec![0u8; n];
    rng.fill(&mut out).unwrap();
    out
}

fn number(rng: &mut TestRng, below: usize) -> usize {
    let mut raw = [0u8; 8];
    rng.fill(&mut raw).unwrap();
    (u64::from_be_bytes(raw) % below as u64) as usize
}

// ---------------------------------------------------------------- round trips

#[test]
fn put_get_update_list_find_round_trip() {
    let mut f = fixture("round trip");
    let a = f.vault.put("memory", "喜歡喝烏龍茶".as_bytes(), 1, &[("topic", "Tea")], &mut f.rng).unwrap();
    let b = f.vault.put("memory", b"walks at seven", 1, &[("topic", "habits")], &mut f.rng).unwrap();
    let t = f.vault.put("connector-token", b"tok_123", 2, &[("service", "calendar")], &mut f.rng).unwrap();

    let got = f.vault.get(&a).unwrap();
    assert_eq!(got.payload.as_slice(), "喜歡喝烏龍茶".as_bytes());
    assert_eq!((got.kind.as_str(), got.version, got.schema), ("memory", 1, 1));
    assert_eq!(got.fields, vec![("topic".to_string(), "tea".to_string())]);

    let mut memory = f.vault.list("memory").unwrap();
    memory.sort();
    let mut expected = vec![a.clone(), b.clone()];
    expected.sort();
    assert_eq!(memory, expected);
    assert_eq!(f.vault.list("connector-token").unwrap(), vec![t.clone()]);
    assert!(f.vault.list("receipt").unwrap().is_empty());

    assert_eq!(f.vault.find("memory", "topic", "  TEA ").unwrap(), vec![a.clone()]);
    assert_eq!(f.vault.find("memory", "topic", "ｔｅａ").unwrap(), vec![a.clone()], "full-width input finds the same record");
    assert!(f.vault.find("memory", "topic", "coffee").unwrap().is_empty());
    assert!(f.vault.find("connector-token", "topic", "tea").unwrap().is_empty(), "an index is per kind");

    assert_eq!(f.vault.update(&a, b"now prefers coffee", 3, &[("topic", "coffee")], &mut f.rng).unwrap(), 2);
    let got = f.vault.get(&a).unwrap();
    assert_eq!((got.payload.as_slice(), got.version, got.schema), (&b"now prefers coffee"[..], 2, 3));
    assert!(f.vault.find("memory", "topic", "tea").unwrap().is_empty());
    assert_eq!(f.vault.find("memory", "topic", "coffee").unwrap(), vec![a.clone()]);
    assert_eq!(f.vault.get("00000000000000000000000000000000").unwrap_err(), VaultError::NotFound);
    assert_eq!(f.vault.get("not an id").unwrap_err(), VaultError::NotFound);
}

#[test]
fn opens_with_the_device_or_the_recovery_key_or_a_passkey() {
    let mut f = fixture("three ways in");
    confirm(&mut f);
    let id = f.vault.put("memory", b"hello", 1, &[], &mut f.rng).unwrap();
    let prf_salt = Vault::<MemoryStore>::new_prf_salt(&mut f.rng).unwrap();
    let prf_output = Secret32::random(&mut f.rng).unwrap();
    let slot = f.vault.add_prf_slot("phone passkey", b"credential-1", &prf_salt, &prf_output, &mut f.rng, NOW).unwrap();
    assert_eq!(f.vault.prf_slots()[0].prf_salt, prf_salt);
    assert_eq!(f.vault.prf_slots()[0].credential_id, b"credential-1");
    let store = f.vault.into_store();

    let by_device = Vault::open_with_device(store.clone(), &f.keystore).unwrap();
    assert_eq!(by_device.get(&id).unwrap().payload.as_slice(), b"hello");
    let by_recovery = Vault::open_with_recovery(store.clone(), &f.recovery).unwrap();
    assert_eq!(by_recovery.get(&id).unwrap().payload.as_slice(), b"hello");
    let by_passkey = Vault::open_with_prf(store.clone(), &slot, &prf_output).unwrap();
    assert_eq!(by_passkey.get(&id).unwrap().payload.as_slice(), b"hello");

    // The words open it in any language, typed by hand.
    for language in Language::ALL {
        let typed = f.recovery.words(language);
        let key = RecoveryKey::from_words(language, &typed).unwrap();
        assert!(Vault::open_with_recovery(store.clone(), &key).is_ok(), "{}", language.name());
    }
}

#[test]
fn every_wrong_key_gives_the_same_answer() {
    let mut f = fixture("wrong keys");
    confirm(&mut f);
    let prf_salt = [7u8; 32];
    let prf_output = Secret32::new([9u8; 32]);
    let slot = f.vault.add_prf_slot("", b"cred", &prf_salt, &prf_output, &mut f.rng, NOW).unwrap();
    let store = f.vault.into_store();
    let wrong_recovery = RecoveryKey::generate(&mut f.rng).unwrap();

    let failures = [
        Vault::open_with_device(store.clone(), &other_keystore("someone else")).unwrap_err(),
        Vault::open_with_recovery(store.clone(), &wrong_recovery).unwrap_err(),
        Vault::open_with_prf(store.clone(), &slot, &Secret32::new([8u8; 32])).unwrap_err(),
        Vault::open_with_prf(store.clone(), "00000000000000000000000000000000", &prf_output).unwrap_err(),
        // Right key, altered header: the same answer again.
        {
            let mut header = Header::parse(store.header.as_ref().unwrap()).unwrap();
            header.updated += 1;
            let mut altered = store.clone();
            altered.header = Some(header.to_bytes());
            Vault::open_with_device(altered, &f.keystore).unwrap_err()
        },
    ];
    for failure in failures {
        assert_eq!(failure, VaultError::OpenFailed);
        assert_eq!(failure.to_string(), "the vault could not be opened");
    }
    // A PRF output does not open the device slot's wrap and vice versa: the kinds are separate.
    assert!(Vault::open_with_recovery(store, &RecoveryKey::from_entropy(prf_output)).is_err());
}

// ---------------------------------------------------------------- tampering

/// A different byte that keeps the text shape (hex stays hex), so the change
/// reaches the cryptographic checks instead of stopping at the parser.
fn mutate(byte: u8) -> u8 {
    match byte {
        b'0'..=b'8' => byte + 1,
        b'9' => b'0',
        b'a'..=b'e' => byte + 1,
        b'f' => b'a',
        other => other ^ 1,
    }
}

#[test]
fn changing_any_byte_of_the_header_stops_it_opening() {
    let mut f = fixture("header bytes");
    confirm(&mut f);
    f.vault.add_prf_slot("", b"cred", &[1u8; 32], &Secret32::new([2u8; 32]), &mut f.rng, NOW).unwrap();
    f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + 5).unwrap(); // so `previous` is covered too
    let store = f.vault.into_store();
    let header = store.header.clone().unwrap();
    assert!(Vault::open_with_device(store.clone(), &f.keystore).is_ok());
    let mut refused_by_parser = 0;
    for at in 0..header.len() {
        let mut altered = store.clone();
        let mut bytes = header.clone();
        bytes[at] = mutate(bytes[at]);
        altered.header = Some(bytes);
        for result in [
            Vault::open_with_device(altered.clone(), &f.keystore).map(|_| ()),
            Vault::open_with_recovery(altered.clone(), &f.recovery).map(|_| ()),
        ] {
            match result {
                Err(VaultError::OpenFailed) => {}
                Err(VaultError::Malformed(_)) | Err(VaultError::UnsupportedVersion) => refused_by_parser += 1,
                other => panic!("byte {at} of the header was changed and the result was {other:?}"),
            }
        }
    }
    assert!(refused_by_parser < header.len(), "most changes must reach the MAC, not just the parser");
}

#[test]
fn a_slot_cannot_be_swapped_moved_or_downgraded() {
    let mut f = fixture("slot swap");
    let phone = other_keystore("phone");
    let request = Vault::<MemoryStore>::enroll_device(f.vault.vault_id(), "phone", &phone, &mut f.rng).unwrap();
    f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap();
    let store = f.vault.into_store();
    let original = Header::parse(store.header.as_ref().unwrap()).unwrap();
    let open = |header: &Header, keystore: &MemoryKeyStore| {
        let mut altered = store.clone();
        altered.header = Some(header.to_bytes());
        Vault::open_with_device(altered, keystore).map(|_| ())
    };
    assert!(open(&original, &f.keystore).is_ok() && open(&original, &phone).is_ok());

    // Give the laptop's slot the phone's wrap (and the reverse).
    let mut swapped = original.clone();
    let (a, b) = (swapped.slots[0].clone(), swapped.slots[1].clone());
    assert_eq!((a.kind.as_str(), b.kind.as_str()), (KIND_DEVICE, KIND_RECOVERY));
    let phone_at = swapped.slots.iter().position(|s| s.label == "phone").unwrap();
    let p = swapped.slots[phone_at].clone();
    for (target, source) in [(0, &p), (phone_at, &a)] {
        swapped.slots[target].eph = source.eph.clone();
        swapped.slots[target].nonce = source.nonce.clone();
        swapped.slots[target].wrapped = source.wrapped.clone();
    }
    assert_eq!(open(&swapped, &f.keystore), Err(VaultError::OpenFailed));
    assert_eq!(open(&swapped, &phone), Err(VaultError::OpenFailed));

    // Point the laptop's slot at the phone's keystore blob and public key: the MAC catches it.
    let mut repointed = original.clone();
    repointed.slots[0].keystore_blob = p.keystore_blob.clone();
    repointed.slots[0].public = p.public.clone();
    assert_eq!(open(&repointed, &phone), Err(VaultError::OpenFailed));

    // Drop a slot, reorder slots, relabel a slot: all caught.
    let mut dropped = original.clone();
    dropped.slots.remove(phone_at);
    assert_eq!(open(&dropped, &f.keystore), Err(VaultError::OpenFailed));
    let mut reordered = original.clone();
    reordered.slots.swap(0, phone_at);
    assert_eq!(open(&reordered, &f.keystore), Err(VaultError::OpenFailed));
    let mut relabelled = original.clone();
    relabelled.slots[phone_at].label = "tablet".into();
    assert_eq!(open(&relabelled, &f.keystore), Err(VaultError::OpenFailed));

    // Turn the recovery slot into a device slot or a passkey slot.
    let mut rekinded = original.clone();
    rekinded.slots[1].kind = KIND_PRF.into();
    assert!(matches!(open(&rekinded, &f.keystore), Err(VaultError::Malformed(_))));

    // Version and epoch games.
    let mut downgraded = original.clone();
    downgraded.version = 0;
    assert_eq!(open(&downgraded, &f.keystore), Err(VaultError::UnsupportedVersion));
    let mut future = original.clone();
    future.version = 2;
    assert_eq!(open(&future, &f.keystore), Err(VaultError::UnsupportedVersion));
    let mut epoch = original.clone();
    epoch.epoch += 1;
    assert_eq!(open(&epoch, &f.keystore), Err(VaultError::OpenFailed));

    // No recovery slot, or two of them: refused before any key is tried.
    let mut no_recovery = original.clone();
    no_recovery.slots.retain(|s| s.kind != KIND_RECOVERY);
    assert_eq!(open(&no_recovery, &f.keystore), Err(VaultError::Malformed("recovery slot")));
    // An unknown field (which the MAC would not cover) is refused.
    let mut json: serde_json::Value = serde_json::from_slice(&original.to_bytes()).unwrap();
    json["note"] = "x".into();
    let mut altered = store.clone();
    altered.header = Some(serde_json::to_vec(&json).unwrap());
    assert_eq!(Vault::open_with_device(altered, &f.keystore).unwrap_err(), VaultError::Malformed("header"));
}

#[test]
fn changing_any_byte_of_a_record_is_detected() {
    let mut f = fixture("record bytes");
    let id = f.vault.put("memory", b"the content", 7, &[("topic", "tea")], &mut f.rng).unwrap();
    let original = f.vault.store().records[&id].clone();
    let mut store = f.vault.into_store();
    let check = |store: &MemoryStore, what: &str| {
        let vault = Vault::open_with_device(store.clone(), &f.keystore).unwrap();
        assert_eq!(vault.get(&id).unwrap_err(), VaultError::Tampered, "{what}");
    };
    // Head: magic, state, version, schema, epoch, nonce, wrapped key. Body: magic, nonce, ciphertext, tag.
    for at in 0..original.head.len() {
        let mut altered = original.clone();
        altered.head[at] ^= 0x01;
        store.records.insert(id.clone(), altered);
        check(&store, &format!("head byte {at}"));
    }
    for at in 0..original.body.len() {
        let mut altered = original.clone();
        altered.body[at] ^= 0x80;
        store.records.insert(id.clone(), altered);
        check(&store, &format!("body byte {at}"));
    }
    // Truncation and extension.
    for (head, body) in [
        (original.head[..original.head.len() - 1].to_vec(), original.body.clone()),
        (original.head.clone(), original.body[..original.body.len() - 1].to_vec()),
        ([original.head.clone(), vec![0]].concat(), original.body.clone()),
        (original.head.clone(), [original.body.clone(), vec![0]].concat()),
        (Vec::new(), original.body.clone()),
        (original.head.clone(), Vec::new()),
    ] {
        store.records.insert(id.clone(), StoredRecord { head, body, ..original.clone() });
        check(&store, "length");
    }
}

#[test]
fn records_cannot_be_swapped_moved_or_brought_from_another_vault() {
    let mut f = fixture("swap records");
    let a = f.vault.put("memory", b"first", 1, &[], &mut f.rng).unwrap();
    let b = f.vault.put("memory", b"second", 1, &[], &mut f.rng).unwrap();
    f.vault.update(&a, b"first, edited", 1, &[], &mut f.rng).unwrap();
    let before_update = {
        let mut g = fixture("swap records"); // same seed: the same vault before the update
        let a2 = g.vault.put("memory", b"first", 1, &[], &mut g.rng).unwrap();
        assert_eq!(a2, a);
        g.vault.store().records[&a].clone()
    };
    let store = f.vault.into_store();
    let (ra, rb) = (store.records[&a].clone(), store.records[&b].clone());
    let expect_tampered = |store: MemoryStore, id: &str, what: &str| {
        let vault = Vault::open_with_device(store, &f.keystore).unwrap();
        assert_eq!(vault.get(id).unwrap_err(), VaultError::Tampered, "{what}");
    };

    let mut swapped = store.clone();
    swapped.records.insert(a.clone(), rb.clone());
    expect_tampered(swapped, &a, "b's record under a's id");

    let mut mixed = store.clone();
    mixed.records.insert(a.clone(), StoredRecord { body: rb.body.clone(), ..ra.clone() });
    expect_tampered(mixed, &a, "a's head with b's body");

    let mut old_body = store.clone();
    old_body.records.insert(a.clone(), StoredRecord { body: before_update.body.clone(), ..ra.clone() });
    expect_tampered(old_body, &a, "new head with the previous version's body");

    let mut moved = store.clone();
    moved.records.insert("ffffffffffffffffffffffffffffffff".into(), ra.clone());
    expect_tampered(moved, "ffffffffffffffffffffffffffffffff", "a's record under a new id");

    // A record from another vault that happens to use the same id.
    let mut g = fixture("another vault");
    let foreign_id = g.vault.put("memory", b"foreign", 1, &[], &mut g.rng).unwrap();
    let foreign = g.vault.store().records[&foreign_id].clone();
    let mut planted = store.clone();
    planted.records.insert(a.clone(), foreign);
    expect_tampered(planted, &a, "a record from another vault");

    // The store files a record under the wrong kind or index tag: list and find refuse.
    let mut f2 = fixture("wrong tags");
    let m = f2.vault.put("memory", b"m", 1, &[("topic", "tea")], &mut f2.rng).unwrap();
    let t = f2.vault.put("token", b"t", 1, &[("topic", "coffee")], &mut f2.rng).unwrap();
    let mut store = f2.vault.into_store();
    let (memory_tag, tea_tag) = (store.records[&m].kind_tag.clone(), store.records[&m].index_tags.clone());
    let token = store.records.get_mut(&t).unwrap();
    token.kind_tag = memory_tag;
    token.index_tags = tea_tag;
    let vault = Vault::open_with_device(store, &f2.keystore).unwrap();
    assert_eq!(vault.list("memory").unwrap_err(), VaultError::Tampered);
    assert_eq!(vault.find("memory", "topic", "tea").unwrap_err(), VaultError::Tampered);
}

#[test]
fn replacing_a_record_with_its_own_older_version_is_not_detected_by_the_vault() {
    // Stated in the format document (section 11): the vault authenticates each
    // record, not the set. Catching a rollback is the job of sync and receipts.
    let mut f = fixture("rollback");
    let id = f.vault.put("memory", b"v1", 1, &[], &mut f.rng).unwrap();
    let v1 = f.vault.store().records[&id].clone();
    f.vault.update(&id, b"v2", 1, &[], &mut f.rng).unwrap();
    let mut store = f.vault.into_store();
    store.records.insert(id.clone(), v1);
    let vault = Vault::open_with_device(store, &f.keystore).unwrap();
    let got = vault.get(&id).unwrap();
    assert_eq!((got.payload.as_slice(), got.version), (&b"v1"[..], 1));
}

// ---------------------------------------------------------------- rotation and lost devices

#[test]
fn rotation_keeps_records_readable_and_rewraps_only_heads() {
    let mut f = fixture("rotation");
    confirm(&mut f);
    let a = f.vault.put("memory", b"alpha", 1, &[("topic", "tea")], &mut f.rng).unwrap();
    let b = f.vault.put("token", &vec![5u8; 100_000], 1, &[("service", "mail")], &mut f.rng).unwrap();
    let gone = f.vault.put("memory", b"gone", 1, &[], &mut f.rng).unwrap();
    f.vault.delete(&gone, &mut f.rng).unwrap();
    let before = f.vault.store().clone();

    let report = f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + 10).unwrap();
    assert_eq!((report.epoch, report.rewrapped, report.skipped), (2, 3, 0));
    assert!(report.new_recovery.is_none(), "the issuing device is still here, so the words are kept");
    assert_eq!(f.vault.epoch(), 2);

    let after = f.vault.store().clone();
    for id in [&a, &b] {
        assert_eq!(after.records[id].body, before.records[id].body, "content is not re-encrypted");
        assert_ne!(after.records[id].head, before.records[id].head, "the record key is re-wrapped");
        assert_ne!(after.records[id].kind_tag, before.records[id].kind_tag, "tags are re-keyed");
        assert_ne!(after.records[id].index_tags, before.records[id].index_tags);
        assert_eq!(after.records[id].head.len(), before.records[id].head.len());
    }
    assert_eq!(f.vault.get(&a).unwrap().payload.as_slice(), b"alpha");
    assert_eq!(f.vault.get(&b).unwrap().payload.len(), 100_000);
    assert_eq!(f.vault.get(&gone).unwrap_err(), VaultError::Deleted);
    assert_eq!(f.vault.find("memory", "topic", "tea").unwrap(), vec![a.clone()]);
    assert_eq!(f.vault.list("token").unwrap(), vec![b.clone()]);

    // Still opens with the device and with the same recovery words; the words check still works.
    let store = f.vault.into_store();
    let mut reopened = Vault::open_with_device(store.clone(), &f.keystore).unwrap();
    assert_eq!(reopened.get(&a).unwrap().payload.as_slice(), b"alpha");
    assert!(Vault::open_with_recovery(store.clone(), &f.recovery).is_ok());
    let word = f.recovery.word(Language::Korean, 5).unwrap();
    let (w1, w2) = (f.recovery.word(Language::Korean, 1).unwrap(), f.recovery.word(Language::Korean, 20).unwrap());
    assert!(reopened.verify_recovery_words(Language::Korean, &[(5, word), (1, w1), (20, w2)], NOW).unwrap());

    // The old header no longer matches the store: its key cannot open re-wrapped heads.
    let mut stale = store.clone();
    stale.header = before.header.clone();
    let old = Vault::open_with_device(stale, &f.keystore).unwrap();
    assert_eq!(old.get(&a).unwrap_err(), VaultError::Tampered);
    assert_eq!(old.check_epoch(2).unwrap_err(), VaultError::Rollback);
    assert!(reopened.check_epoch(2).is_ok());

    // Renew on request: new words, old ones stop working.
    let report = reopened.rotate(RecoveryPolicy::Renew, &mut f.rng, NOW + 20).unwrap();
    let new_words = report.new_recovery.expect("new recovery key");
    assert!(!reopened.recovery_confirmed());
    let store = reopened.into_store();
    assert_eq!(Vault::open_with_recovery(store.clone(), &f.recovery).unwrap_err(), VaultError::OpenFailed);
    let by_new = Vault::open_with_recovery(store, &new_words).unwrap();
    assert_eq!(by_new.get(&a).unwrap().payload.as_slice(), b"alpha");
}

#[test]
fn a_failed_rotation_changes_nothing() {
    let mut f = fixture("atomic rotation");
    let id = f.vault.put("memory", b"alpha", 1, &[], &mut f.rng).unwrap();
    let mut store = f.vault.into_store();
    store.fail_next_apply = true;
    let before = store.clone();
    let mut vault = Vault::open_with_device(store, &f.keystore).unwrap();
    assert_eq!(vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW).unwrap_err(), VaultError::Store);
    assert_eq!(vault.epoch(), 1);
    assert_eq!(vault.get(&id).unwrap().payload.as_slice(), b"alpha");
    let mut expected = before;
    expected.fail_next_apply = false;
    assert_eq!(*vault.store(), expected);
}

#[test]
fn lost_device_flow() {
    // Laptop creates the vault; the phone is paired; the phone is lost.
    let mut f = fixture("lost device");
    confirm(&mut f);
    let phone_keys = other_keystore("phone keystore");
    let request = Vault::<MemoryStore>::enroll_device(f.vault.vault_id(), "phone", &phone_keys, &mut f.rng).unwrap();
    assert!(!format!("{request:?}").is_empty());
    let phone_slot = f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap();
    assert_eq!(
        f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap_err(),
        VaultError::Invalid("device label already used")
    );
    let old_record = f.vault.put("memory", b"before the loss", 1, &[("topic", "tea")], &mut f.rng).unwrap();

    // What the phone holds at the moment it is lost: a copy of everything, and its own keystore.
    let phone_copy = f.vault.store().clone();
    assert!(Vault::open_with_device(phone_copy.clone(), &phone_keys).is_ok());

    let report = f.vault.lost_device(&phone_slot, RecoveryPolicy::Keep, &mut f.rng, NOW + 100).unwrap();
    assert!(report.new_recovery.is_none());
    assert!(f.vault.slots().iter().all(|s| s.label != "phone"));
    let new_record = f.vault.put("memory", b"after the loss", 1, &[("topic", "tea")], &mut f.rng).unwrap();
    f.vault.update(&old_record, b"edited after the loss", 1, &[], &mut f.rng).unwrap();
    let current = f.vault.store().clone();

    // The phone's key opens nothing in the current vault.
    assert_eq!(Vault::open_with_device(current.clone(), &phone_keys).unwrap_err(), VaultError::OpenFailed);
    // Even with its old header in front of the new records, it reads none of them.
    let mut franken = current.clone();
    franken.header = phone_copy.header.clone();
    let thief = Vault::open_with_device(franken, &phone_keys).unwrap();
    assert_eq!(thief.get(&new_record).unwrap_err(), VaultError::Tampered);
    assert_eq!(thief.get(&old_record).unwrap_err(), VaultError::Tampered);
    assert!(thief.list("memory").unwrap().is_empty(), "the new index tags mean nothing to the old key");
    // Said plainly: what the phone already copied, it can still read.
    let thief = Vault::open_with_device(phone_copy, &phone_keys).unwrap();
    assert_eq!(thief.get(&old_record).unwrap().payload.as_slice(), b"before the loss");

    // The laptop and the recovery words carry on.
    assert_eq!(f.vault.get(&new_record).unwrap().payload.as_slice(), b"after the loss");
    assert!(Vault::open_with_recovery(current, &f.recovery).is_ok());
    assert_eq!(
        f.vault.lost_device("00000000000000000000000000000000", RecoveryPolicy::Keep, &mut f.rng, NOW).unwrap_err(),
        VaultError::NotFound
    );
}

#[test]
fn losing_the_device_that_made_the_recovery_key_forces_new_words() {
    let mut f = fixture("issuer lost");
    let phone_keys = other_keystore("phone keystore");
    let request = Vault::<MemoryStore>::enroll_device(f.vault.vault_id(), "phone", &phone_keys, &mut f.rng).unwrap();
    f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap();
    let id = f.vault.put("memory", b"m", 1, &[], &mut f.rng).unwrap();
    let laptop_slot = f.vault.slots().into_iter().find(|s| s.label == "laptop").unwrap().id;
    let recovery_slot = f.vault.slots().into_iter().find(|s| s.kind == KIND_RECOVERY).unwrap().id;
    assert_eq!(
        f.vault.lost_device(&recovery_slot, RecoveryPolicy::Keep, &mut f.rng, NOW).unwrap_err(),
        VaultError::Invalid("the recovery slot cannot be removed")
    );
    assert_eq!(
        f.vault.remove_device_slot(&recovery_slot, NOW).unwrap_err(),
        VaultError::Invalid("the recovery slot cannot be removed")
    );

    // The phone reports the laptop lost. The laptop made the words and may hold a pending copy.
    let mut phone = Vault::open_with_device(f.vault.into_store(), &phone_keys).unwrap();
    let report = phone.lost_device(&laptop_slot, RecoveryPolicy::Keep, &mut f.rng, NOW + 1).unwrap();
    let new_words = report.new_recovery.expect("Keep is overridden when the issuer is gone");
    let store = phone.into_store();
    assert_eq!(Vault::open_with_recovery(store.clone(), &f.recovery).unwrap_err(), VaultError::OpenFailed);
    assert_eq!(Vault::open_with_device(store.clone(), &f.keystore).unwrap_err(), VaultError::OpenFailed);
    assert_eq!(Vault::open_with_recovery(store, &new_words).unwrap().get(&id).unwrap().payload.as_slice(), b"m");
}

#[test]
fn removing_a_slot_without_rotating_is_only_bookkeeping() {
    let mut f = fixture("remove only");
    let phone_keys = other_keystore("phone keystore");
    let request = Vault::<MemoryStore>::enroll_device(f.vault.vault_id(), "phone", &phone_keys, &mut f.rng).unwrap();
    let slot = f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap();
    f.vault.remove_device_slot(&slot, NOW + 1).unwrap();
    assert_eq!(f.vault.epoch(), 1, "no new key");
    assert_eq!(Vault::open_with_device(f.vault.store().clone(), &phone_keys).unwrap_err(), VaultError::OpenFailed);
    assert_eq!(f.vault.remove_device_slot(&slot, NOW).unwrap_err(), VaultError::NotFound);
}

#[test]
fn records_written_under_an_older_key_stay_readable_after_rotation() {
    // The phone has not yet seen the rotation and writes a record under epoch 1;
    // the record reaches the laptop afterwards (as sync would deliver it).
    let mut f = fixture("straggler");
    confirm(&mut f);
    let phone_keys = other_keystore("phone keystore");
    let request = Vault::<MemoryStore>::enroll_device(f.vault.vault_id(), "phone", &phone_keys, &mut f.rng).unwrap();
    f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap();
    let mut phone = Vault::open_with_device(f.vault.store().clone(), &phone_keys).unwrap();
    let late = phone.put("memory", b"written late", 1, &[("topic", "tea")], &mut f.rng).unwrap();
    let late_record = phone.store().records[&late].clone();

    f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + 1).unwrap();
    f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + 2).unwrap();
    let mut store = f.vault.into_store();
    store.records.insert(late.clone(), late_record);
    let mut laptop = Vault::open_with_device(store, &f.keystore).unwrap();
    assert_eq!(laptop.get(&late).unwrap().payload.as_slice(), b"written late");
    assert!(laptop.list("memory").unwrap().is_empty(), "its tags are from the old key until the next re-wrap");
    let report = laptop.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + 3).unwrap();
    assert_eq!((report.rewrapped, report.skipped), (1, 0));
    assert_eq!(laptop.list("memory").unwrap(), vec![late.clone()]);
    assert_eq!(laptop.find("memory", "topic", "tea").unwrap(), vec![late]);
}

#[test]
fn rotation_leaves_unreadable_heads_alone_and_counts_them() {
    let mut f = fixture("skipped heads");
    let good = f.vault.put("memory", b"good", 1, &[], &mut f.rng).unwrap();
    let bad = f.vault.put("memory", b"bad", 1, &[], &mut f.rng).unwrap();
    let mut store = f.vault.into_store();
    store.records.get_mut(&bad).unwrap().head[40] ^= 1;
    let mut vault = Vault::open_with_device(store, &f.keystore).unwrap();
    let report = vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW).unwrap();
    assert_eq!((report.rewrapped, report.skipped), (1, 1));
    assert_eq!(vault.get(&good).unwrap().payload.as_slice(), b"good");
    assert_eq!(vault.get(&bad).unwrap_err(), VaultError::Tampered);
}

#[test]
fn only_the_newest_eight_retired_keys_are_kept() {
    let mut f = fixture("many rotations");
    let id = f.vault.put("memory", b"kept", 1, &[], &mut f.rng).unwrap();
    for i in 0..10 {
        f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + i).unwrap();
    }
    assert_eq!(f.vault.epoch(), 11);
    let header = Header::parse(f.vault.store().header.as_ref().unwrap()).unwrap();
    assert_eq!(header.previous.iter().map(|p| p.epoch).collect::<Vec<_>>(), (3..=10).collect::<Vec<_>>());
    let store = f.vault.into_store();
    assert_eq!(Vault::open_with_device(store, &f.keystore).unwrap().get(&id).unwrap().payload.as_slice(), b"kept");
}

// ---------------------------------------------------------------- padding

#[test]
fn padding_buckets() {
    let table = [
        (0, 256), (1, 256), (256, 256), (257, 512), (512, 512), (513, 1024), (4096, 4096), (4097, 8192),
        (65536, 65536), (65537, 131072), (131072, 131072), (131073, 196608), (1_000_000, 1_048_576),
    ];
    for (n, bucket) in table {
        assert_eq!(bucket_size(n), bucket, "{n}");
    }
    for (n, bucket) in [(0, 128), (1, 128), (128, 128), (129, 192), (192, 192), (193, 256)] {
        assert_eq!(head_bucket(n), bucket, "{n}");
    }

    let mut f = fixture("padding");
    let mut size_of = |len: usize, fields: &[(&str, &str)]| {
        let id = f.vault.put("memory", &vec![b'x'; len], 1, fields, &mut f.rng).unwrap();
        let record = f.vault.store().records[&id].clone();
        (record.head.len(), record.body.len())
    };
    // body = magic 4 + nonce 24 + bucket(len + 4) + tag 16; head = 49 + bucket + tag 16.
    assert_eq!(size_of(0, &[]), (49 + 128 + 16, 44 + 256));
    assert_eq!(size_of(1, &[]), size_of(252, &[]), "a one-byte and a 252-byte record look the same");
    assert_eq!(size_of(253, &[]).1, 44 + 512);
    assert_eq!(size_of(70_000, &[]).1, 44 + 131_072);
    assert_eq!(size_of(5, &[("topic", "tea")]).0, size_of(5, &[("topic", "a much longer value, still one bucket")]).0);
    // 32 (key) + 8 (kind) + 2 (count) + 7 (name) + 2 + 199 (value) = 250, + 2 = 252 -> 256.
    assert_eq!(size_of(5, &[("topic", &"long ".repeat(40))]).0, 49 + 256 + 16);
    assert_eq!(size_of(5, &[("topic", &"long ".repeat(41))]).0, 49 + 320 + 16);
}

// ---------------------------------------------------------------- deletion

#[test]
fn deleted_records_are_tombstones_and_their_key_is_gone() {
    let mut f = fixture("delete");
    let id = f.vault.put("memory", b"to be forgotten", 1, &[("topic", "tea")], &mut f.rng).unwrap();
    let keep = f.vault.put("memory", b"kept", 1, &[("topic", "tea")], &mut f.rng).unwrap();
    let old = f.vault.store().records[&id].clone();
    f.vault.delete(&id, &mut f.rng).unwrap();
    f.vault.delete(&id, &mut f.rng).unwrap(); // idempotent

    assert_eq!(f.vault.get(&id).unwrap_err(), VaultError::Deleted);
    assert_eq!(f.vault.update(&id, b"x", 1, &[], &mut f.rng).unwrap_err(), VaultError::Deleted);
    assert_eq!(f.vault.list("memory").unwrap(), vec![keep.clone()]);
    assert_eq!(f.vault.find("memory", "topic", "tea").unwrap(), vec![keep.clone()]);
    let tombstone = f.vault.store().records[&id].clone();
    assert!(tombstone.body.is_empty() && tombstone.kind_tag.is_empty() && tombstone.index_tags.is_empty());
    assert_eq!(tombstone.head.len(), 49 + 16, "a tombstone holds no wrapped key, only a tag");
    assert_eq!(tombstone.head[4], 1);

    // The old ciphertext put back beside the tombstone opens nothing: its key was only in the old head.
    let mut store = f.vault.store().clone();
    store.records.get_mut(&id).unwrap().body = old.body.clone();
    let vault = Vault::open_with_device(store.clone(), &f.keystore).unwrap();
    assert_eq!(vault.get(&id).unwrap_err(), VaultError::Deleted);
    // A tombstone cannot be forged from a live head, nor a live head from a tombstone.
    let mut forged = old.clone();
    forged.head[4] = 1;
    store.records.insert(id.clone(), forged);
    assert_eq!(Vault::open_with_device(store.clone(), &f.keystore).unwrap().get(&id).unwrap_err(), VaultError::Tampered);
    let mut revived = tombstone.clone();
    revived.head[4] = 0;
    revived.body = old.body.clone();
    store.records.insert(id.clone(), revived);
    assert_eq!(Vault::open_with_device(store.clone(), &f.keystore).unwrap().get(&id).unwrap_err(), VaultError::Tampered);

    // Stated limit: a complete old copy (head and body) still opens. Deleting
    // shreds the key in this store, not in copies made before.
    store.records.insert(id.clone(), old);
    assert_eq!(Vault::open_with_device(store, &f.keystore).unwrap().get(&id).unwrap().payload.as_slice(), b"to be forgotten");

    // Purge removes the tombstone itself; a live record cannot be purged.
    assert_eq!(f.vault.purge(&keep).unwrap_err(), VaultError::Invalid("record is not deleted"));
    f.vault.purge(&id).unwrap();
    assert_eq!(f.vault.get(&id).unwrap_err(), VaultError::NotFound);
    assert_eq!(f.vault.delete(&id, &mut f.rng).unwrap_err(), VaultError::NotFound);
}

// ---------------------------------------------------------------- export and import

fn exported(seed: &str) -> (Fixture, Vec<String>, Vec<u8>) {
    let mut f = fixture(seed);
    confirm(&mut f);
    f.vault.add_prf_slot("passkey", b"cred", &[3u8; 32], &Secret32::new([4u8; 32]), &mut f.rng, NOW).unwrap();
    let ids = vec![
        f.vault.put("memory", "烏龍茶".as_bytes(), 1, &[("topic", "tea")], &mut f.rng).unwrap(),
        f.vault.put("token", &vec![1u8; 70_000], 2, &[("service", "mail")], &mut f.rng).unwrap(),
        f.vault.put("memory", b"deleted", 1, &[], &mut f.rng).unwrap(),
    ];
    f.vault.delete(&ids[2], &mut f.rng).unwrap();
    f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW + 1).unwrap();
    let archive = f.vault.export(NOW + 2).unwrap();
    (f, ids, archive)
}

#[test]
fn export_then_import_on_a_new_device() {
    let (mut f, ids, archive) = exported("export");
    assert!(archive.starts_with(b"age-encryption.org/v1\n"));
    let new_keys = other_keystore("new laptop");
    let restored =
        Vault::import(&archive, &f.recovery, MemoryStore::new(), "new laptop", &new_keys, &mut f.rng, NOW + 50).unwrap();
    assert_eq!(restored.vault_id(), f.vault.vault_id());
    assert_eq!(restored.epoch(), 2);
    assert_eq!(restored.get(&ids[0]).unwrap().payload.as_slice(), "烏龍茶".as_bytes());
    assert_eq!(restored.get(&ids[1]).unwrap().payload.len(), 70_000);
    assert_eq!(restored.get(&ids[2]).unwrap_err(), VaultError::Deleted);
    assert_eq!(restored.find("memory", "topic", "tea").unwrap(), vec![ids[0].clone()]);
    assert_eq!(restored.list("token").unwrap(), vec![ids[1].clone()]);

    // The archive carries no device slot: the old laptop's keystore opens nothing in the restored vault.
    let kinds: Vec<(String, String)> = restored.slots().into_iter().map(|s| (s.kind, s.label)).collect();
    assert_eq!(
        kinds,
        vec![
            (KIND_DEVICE.to_string(), "new laptop".to_string()),
            (KIND_RECOVERY.to_string(), String::new()),
            (KIND_PRF.to_string(), "passkey".to_string()),
        ]
    );
    let store = restored.into_store();
    assert_eq!(Vault::open_with_device(store.clone(), &f.keystore).unwrap_err(), VaultError::OpenFailed);
    assert!(Vault::open_with_device(store.clone(), &new_keys).is_ok());
    assert!(Vault::open_with_recovery(store.clone(), &f.recovery).is_ok());
    let prf_slot = Header::parse(store.header.as_ref().unwrap()).unwrap().slots.iter().find(|s| s.kind == KIND_PRF).unwrap().id.clone();
    assert!(Vault::open_with_prf(store, &prf_slot, &Secret32::new([4u8; 32])).is_ok());
}

#[test]
fn import_refuses_a_wrong_key_a_damaged_archive_and_a_used_store() {
    let (mut f, _, archive) = exported("import failures");
    let keys = other_keystore("new laptop");
    let mut attempt = |archive: &[u8], recovery: &RecoveryKey, store: MemoryStore| {
        let before = store.clone();
        let result = Vault::import(archive, recovery, store, "new laptop", &keys, &mut f.rng, NOW);
        (result.map(|_| ()).unwrap_err(), before)
    };
    let wrong = RecoveryKey::from_entropy(Secret32::new([0x55; 32]));
    assert_eq!(attempt(&archive, &wrong, MemoryStore::new()).0, VaultError::ImportFailed);

    // Flip one byte in many places (header stanza, MAC, every part of the payload), truncate, extend.
    let mut positions: Vec<usize> = (0..archive.len()).step_by(archive.len() / 150).collect();
    positions.extend([archive.len() - 1, archive.len() - 17, archive.len() / 2]);
    for at in positions {
        let mut damaged = archive.clone();
        damaged[at] ^= 0x04;
        assert_eq!(attempt(&damaged, &f.recovery, MemoryStore::new()).0, VaultError::ImportFailed, "byte {at}");
    }
    assert_eq!(attempt(&archive[..archive.len() - 1], &f.recovery, MemoryStore::new()).0, VaultError::ImportFailed);
    assert_eq!(attempt(&archive[..archive.len() / 2], &f.recovery, MemoryStore::new()).0, VaultError::ImportFailed);
    assert_eq!(attempt(&[archive.clone(), vec![0]].concat(), &f.recovery, MemoryStore::new()).0, VaultError::ImportFailed);
    assert_eq!(attempt(b"", &f.recovery, MemoryStore::new()).0, VaultError::ImportFailed);

    // A store that already holds something is never written over.
    let used = fixture("used store").vault.into_store();
    let (error, before) = attempt(&archive, &f.recovery, used);
    assert_eq!(error, VaultError::Exists);
    assert!(before.header.is_some());

    // The store fails at the last moment: nothing is left half written.
    let mut failing = MemoryStore::new();
    failing.fail_next_apply = true;
    assert_eq!(attempt(&archive, &f.recovery, failing).0, VaultError::Store);
}

#[test]
fn a_backup_opens_only_with_the_words_that_were_current_when_it_was_made() {
    let (mut f, ids, archive) = exported("old backups");
    let new_words = f.vault.renew_recovery(&mut f.rng, NOW + 3).unwrap();
    let keys = other_keystore("new laptop");
    assert_eq!(
        Vault::import(&archive, &new_words, MemoryStore::new(), "n", &keys, &mut f.rng, NOW).map(|_| ()).unwrap_err(),
        VaultError::ImportFailed
    );
    let restored = Vault::import(&archive, &f.recovery, MemoryStore::new(), "n", &keys, &mut f.rng, NOW).unwrap();
    assert_eq!(restored.get(&ids[0]).unwrap().payload.as_slice(), "烏龍茶".as_bytes());
    // A fresh export is for the new words.
    let fresh = f.vault.export(NOW + 4).unwrap();
    assert!(Vault::import(&fresh, &new_words, MemoryStore::new(), "n", &keys, &mut f.rng, NOW).is_ok());
    assert!(Vault::import(&fresh, &f.recovery, MemoryStore::new(), "n", &keys, &mut f.rng, NOW).is_err());
}

// ---------------------------------------------------------------- recovery words

#[test]
fn verify_recovery_words() {
    let mut f = fixture("verify words");
    assert!(!f.vault.recovery_confirmed());
    let positions = Vault::<MemoryStore>::recovery_challenge(&mut f.rng).unwrap();
    assert!(positions[0] < positions[1] && positions[1] < positions[2] && positions[0] >= 1 && positions[2] <= 24);
    let word = |language: Language, p: u8| f.recovery.word(language, usize::from(p)).unwrap();

    // A passkey may not be added before the words are confirmed.
    assert_eq!(
        f.vault.add_prf_slot("", b"c", &[0u8; 32], &Secret32::new([1u8; 32]), &mut f.rng, NOW).unwrap_err(),
        VaultError::Invalid("recovery key not confirmed")
    );

    // Not a word, a word from another language's list, two words in one box: no.
    let (a, b, c) = (word(Language::English, positions[0]), word(Language::English, positions[1]), word(Language::English, positions[2]));
    let p = positions;
    for wrong in ["zzzz", "", word(Language::Japanese, p[0]), "abandon ability"] {
        assert!(!f.vault.verify_recovery_words(Language::English, &[(p[0], wrong), (p[1], b), (p[2], c)], NOW).unwrap());
    }
    assert!(!f.vault.recovery_confirmed());
    // Wrong shape of question.
    for entries in [vec![(p[0], a), (p[1], b)], vec![(p[0], a), (p[0], a), (p[2], c)], vec![(0, a), (p[1], b), (p[2], c)], vec![(25, a), (p[1], b), (p[2], c)]] {
        assert!(matches!(f.vault.verify_recovery_words(Language::English, &entries, NOW), Err(VaultError::Invalid(_))));
    }

    // Every other word of the list in one position: about one in sixteen passes
    // (the check value is 4 bits, by design), and the right word always does.
    let right = Language::English.index_of(a).unwrap();
    let mut accepted = 0;
    for index in 0..2048u16 {
        let candidate = Language::English.word(index).unwrap();
        let ok = f.vault.verify_recovery_words(Language::English, &[(p[0], candidate), (p[1], b), (p[2], c)], NOW + 9).unwrap();
        if index == right {
            assert!(ok);
        } else if ok {
            accepted += 1;
        }
    }
    assert!((80..=180).contains(&accepted), "{accepted} of 2047 wrong words passed; expected about 128");
    assert!(f.vault.recovery_confirmed(), "the right words marked it confirmed");

    // Typed in another language, with a stray space and in composed form.
    let zh: Vec<&str> = p.iter().map(|&q| word(Language::ChineseTraditional, q)).collect();
    assert!(f.vault.verify_recovery_words(Language::ChineseTraditional, &[(p[0], zh[0]), (p[1], zh[1]), (p[2], zh[2])], NOW).unwrap());
    let ko: Vec<String> = p.iter().map(|&q| format!(" {} ", word(Language::Korean, q).nfc().collect::<String>())).collect();
    assert!(f.vault.verify_recovery_words(Language::Korean, &[(p[0], &ko[0]), (p[1], &ko[1]), (p[2], &ko[2])], NOW).unwrap());
    // Positions in any order.
    assert!(f.vault.verify_recovery_words(Language::English, &[(p[2], c), (p[0], a), (p[1], b)], NOW).unwrap());

    // The confirmation is in the header and survives reopening; new words are unconfirmed again.
    let store = f.vault.into_store();
    let mut vault = Vault::open_with_device(store, &f.keystore).unwrap();
    assert!(vault.recovery_confirmed());
    let renewed = vault.renew_recovery(&mut f.rng, NOW).unwrap();
    assert!(!vault.recovery_confirmed());
    assert_eq!(Vault::open_with_recovery(vault.store().clone(), &f.recovery).unwrap_err(), VaultError::OpenFailed);
    assert!(Vault::open_with_recovery(vault.store().clone(), &renewed).is_ok());
}

#[test]
fn recovery_challenge_positions_are_spread_over_all_24() {
    let mut rng = TestRng::new(b"challenge");
    let mut seen = [0u32; 24];
    for _ in 0..2000 {
        for position in Vault::<MemoryStore>::recovery_challenge(&mut rng).unwrap() {
            seen[usize::from(position) - 1] += 1;
        }
    }
    assert!(seen.iter().all(|&n| (170..=330).contains(&n)), "{seen:?}");
}

#[test]
fn pending_recovery_key_is_held_by_the_device_keystore() {
    let f = fixture("pending");
    let blob = f.recovery.to_pending(&f.keystore).unwrap();
    let back = RecoveryKey::from_pending(&f.keystore, &blob).unwrap();
    assert_eq!(*back.words(Language::English), *f.recovery.words(Language::English));
    assert!(RecoveryKey::from_pending(&other_keystore("x"), &blob).is_err());
}

// ---------------------------------------------------------------- mnemonics

const ZERO_24: &str = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";

#[test]
fn mnemonic_errors() {
    assert!(RecoveryKey::from_words(Language::English, ZERO_24).is_ok());
    // Last word changed: checksum.
    let bad = ZERO_24.replace(" art", " zoo");
    assert_eq!(RecoveryKey::from_words(Language::English, &bad).unwrap_err(), MnemonicError::Checksum);
    // Not a word: position reported (1-based), never the word.
    let typo = ZERO_24.replacen("abandon abandon", "abandon abandn", 1);
    let error = RecoveryKey::from_words(Language::English, &typo).unwrap_err();
    assert_eq!(error, MnemonicError::UnknownWord(2));
    assert!(!error.to_string().contains("abandn"));
    // Wrong count, including a valid 12-word mnemonic: a recovery key is 24 words.
    for text in ["", "abandon", "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"] {
        assert_eq!(RecoveryKey::from_words(Language::English, text).unwrap_err(), MnemonicError::WordCount, "{text:?}");
    }
    // Two different words swapped: detected (for this key).
    let key = RecoveryKey::from_entropy(Secret32::new([0x42; 32]));
    let words = key.words(Language::English);
    let mut list: Vec<&str> = words.split(' ').collect();
    assert_ne!(list[0], list[1]);
    list.swap(0, 1);
    assert_eq!(RecoveryKey::from_words(Language::English, &list.join(" ")).unwrap_err(), MnemonicError::Checksum);
}

#[test]
fn wrong_language_list() {
    let key = RecoveryKey::from_entropy(Secret32::new([0x17; 32]));
    for typed_in in Language::ALL {
        let text = key.words(typed_in);
        for read_as in Language::ALL {
            let result = RecoveryKey::from_words(read_as, &text);
            if typed_in == read_as {
                assert_eq!(*result.unwrap().words(Language::English), *key.words(Language::English));
            } else {
                assert_eq!(result.unwrap_err(), MnemonicError::UnknownWord(1), "{} read as {}", typed_in.name(), read_as.name());
            }
        }
        let (found, language) = RecoveryKey::from_words_any(&text).unwrap();
        assert_eq!(language, typed_in);
        assert_eq!(*found.words(Language::English), *key.words(Language::English));
    }
    // The language is worked out from the first word; the error is then about that language.
    let broken = key.words(Language::Korean).replacen(' ', " 가가가 ", 1);
    assert!(matches!(RecoveryKey::from_words_any(&broken).unwrap_err(), MnemonicError::UnknownWord(2) | MnemonicError::WordCount));
    assert_eq!(RecoveryKey::from_words_any("12345 67890").unwrap_err(), MnemonicError::UnknownWord(1));
}

#[test]
fn unicode_normalisation_of_typed_words() {
    let key = RecoveryKey::from_entropy(Secret32::new([0xA5; 32]));
    let english = key.words(Language::English).to_string();
    let same = |language: Language, typed: &str| {
        let read = RecoveryKey::from_words(language, typed).unwrap_or_else(|e| panic!("{}: {e}", language.name()));
        assert_eq!(*read.words(Language::English), english, "{}", language.name());
    };

    // English: capitals, tabs, newlines, full-width letters.
    same(Language::English, &english.to_uppercase().replace(' ', "\t\n  "));
    let full_width: String = english.chars().map(|c| if c.is_ascii_lowercase() { char::from_u32(c as u32 + 0xFEE0).unwrap() } else { c }).collect();
    same(Language::English, &full_width);

    // Japanese: shown with ideographic spaces; typed composed (NFC), as an input method produces it.
    let japanese = key.words(Language::Japanese).to_string();
    assert!(japanese.contains('\u{3000}'));
    let decomposed: String = japanese.nfkd().collect::<String>().replace(' ', "\u{3000}");
    assert_ne!(decomposed, japanese, "shown composed; the official list is decomposed");
    assert_eq!(japanese.nfc().collect::<String>(), japanese);
    same(Language::Japanese, &japanese);
    same(Language::Japanese, &decomposed);
    same(Language::Japanese, &japanese.replace('\u{3000}', " "));

    // Korean: composed Hangul syllables.
    let korean = key.words(Language::Korean).to_string();
    let decomposed: String = korean.nfd().collect();
    assert_ne!(decomposed, korean, "shown as composed syllables; the official list is decomposed jamo");
    same(Language::Korean, &korean);
    same(Language::Korean, &decomposed);

    // Traditional Chinese: with spaces, without, with ideographic spaces, in lines of six.
    let chinese = key.words(Language::ChineseTraditional).to_string();
    assert_eq!(chinese.chars().filter(|c| !c.is_whitespace()).count(), 24);
    same(Language::ChineseTraditional, &chinese);
    same(Language::ChineseTraditional, &chinese.replace(' ', ""));
    same(Language::ChineseTraditional, &chinese.replace(' ', "\u{3000}"));
    let lines: Vec<String> = chinese.replace(' ', "").chars().collect::<Vec<_>>().chunks(6).map(|c| c.iter().collect()).collect();
    same(Language::ChineseTraditional, &lines.join("\n"));
    // A Simplified-only character is not in the Traditional list.
    let simplified = chinese.replace(' ', "").replacen(|_: char| true, "这", 1);
    assert_eq!(RecoveryKey::from_words(Language::ChineseTraditional, &simplified).unwrap_err(), MnemonicError::UnknownWord(1));
}

// ---------------------------------------------------------------- locking and hygiene

#[test]
fn a_locked_vault_does_nothing_until_unlocked() {
    let mut f = fixture("lock");
    confirm(&mut f);
    let id = f.vault.put("memory", b"m", 1, &[("topic", "tea")], &mut f.rng).unwrap();
    f.vault.lock();
    assert!(f.vault.is_locked());
    let locked = VaultError::Locked;
    assert_eq!(f.vault.get(&id).unwrap_err(), locked);
    assert_eq!(f.vault.list("memory").unwrap_err(), locked);
    assert_eq!(f.vault.find("memory", "topic", "tea").unwrap_err(), locked);
    assert_eq!(f.vault.put("memory", b"x", 1, &[], &mut f.rng).unwrap_err(), locked);
    assert_eq!(f.vault.update(&id, b"x", 1, &[], &mut f.rng).unwrap_err(), locked);
    assert_eq!(f.vault.delete(&id, &mut f.rng).unwrap_err(), locked);
    assert_eq!(f.vault.purge(&id).unwrap_err(), locked);
    assert_eq!(f.vault.export(NOW).unwrap_err(), locked);
    assert_eq!(f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW).map(|_| ()).unwrap_err(), locked);
    assert_eq!(f.vault.lost_device("x", RecoveryPolicy::Keep, &mut f.rng, NOW).map(|_| ()).unwrap_err(), locked);
    assert_eq!(f.vault.renew_recovery(&mut f.rng, NOW).map(|_| ()).unwrap_err(), locked);
    assert_eq!(f.vault.verify_recovery_words(Language::English, &[(1, "a"), (2, "b"), (3, "c")], NOW).unwrap_err(), locked);
    assert_eq!(f.vault.keyed_ref("record", b"x").unwrap_err(), locked);
    assert_eq!(f.vault.remove_device_slot("x", NOW).unwrap_err(), locked);
    assert_eq!(f.vault.add_prf_slot("", b"c", &[0; 32], &Secret32::new([0; 32]), &mut f.rng, NOW).unwrap_err(), locked);
    let request = Vault::<MemoryStore>::enroll_device(f.vault.vault_id(), "phone", &f.keystore, &mut f.rng).unwrap();
    assert_eq!(f.vault.add_device_slot(&request, &mut f.rng, NOW).unwrap_err(), locked);

    // A failed unlock leaves it locked; the right key unlocks it.
    assert_eq!(f.vault.unlock_with_device(&other_keystore("x")).unwrap_err(), VaultError::OpenFailed);
    assert!(f.vault.is_locked());
    f.vault.unlock_with_device(&f.keystore).unwrap();
    assert_eq!(f.vault.get(&id).unwrap().payload.as_slice(), b"m");
    // A failed unlock of an unlocked vault locks it (it does not stay open on the old key).
    assert!(f.vault.unlock_with_recovery(&RecoveryKey::from_entropy(Secret32::new([1; 32]))).is_err());
    assert!(f.vault.is_locked());
    f.vault.unlock_with_recovery(&f.recovery).unwrap();
    assert!(!f.vault.is_locked());
}

#[test]
fn auto_lock_after_idle_time() {
    let mut f = fixture("auto lock");
    assert!(!f.vault.lock_if_idle(NOW + 1_000_000), "off unless set");
    f.vault.set_auto_lock(Some(300), NOW);
    assert!(!f.vault.lock_if_idle(NOW + 299));
    f.vault.note_activity(NOW + 299);
    assert!(!f.vault.lock_if_idle(NOW + 598));
    assert!(f.vault.lock_if_idle(NOW + 599));
    assert!(f.vault.is_locked());
    assert!(f.vault.lock_if_idle(NOW), "stays locked, even if the clock goes back");
}

#[test]
fn secrets_are_wiped_and_never_printed() {
    let mut secret = Secret32::new([0xAB; 32]);
    assert_eq!(format!("{secret:?}"), "Secret32(<redacted>)");
    secret.zeroize();
    assert_eq!(secret.expose(), &[0u8; 32]);

    let mut f = fixture("no secrets in debug");
    confirm(&mut f);
    let canary = "CANARY-content-7f3a";
    let field = "canary-field-value-91c2";
    let id = f.vault.put("memory", canary.as_bytes(), 1, &[("topic", field)], &mut f.rng).unwrap();
    let record = f.vault.get(&id).unwrap();
    let first_three = |key: &RecoveryKey| key.words(Language::English).split(' ').take(3).collect::<Vec<_>>().join(" ");
    let old_words = first_three(&f.recovery);
    let rotation = f.vault.rotate(RecoveryPolicy::Renew, &mut f.rng, NOW).unwrap();
    let new_words = first_three(rotation.new_recovery.as_ref().unwrap());
    let (pass_store, key_file) = PassphraseKeyStore::create("correct horse battery", Argon2Params::MINIMUM, &mut f.rng).unwrap();

    let mut printed = vec![
        format!("{:?}", f.vault),
        format!("{:?}", f.recovery),
        format!("{:?}", f.keystore),
        format!("{record:?}"),
        format!("{rotation:?}"),
        format!("{pass_store:?}"),
        format!("{:?}", f.rng),
        format!("{:?}", f.vault.slots()),
    ];
    for error in [
        VaultError::Locked, VaultError::OpenFailed, VaultError::Tampered, VaultError::Deleted, VaultError::ImportFailed,
        VaultError::Mnemonic(MnemonicError::UnknownWord(3)), VaultError::Invalid("field"), VaultError::Malformed("slot"),
    ] {
        printed.push(format!("{error:?} {error}"));
    }
    let all = printed.join("\n");
    for secret in [canary, field, old_words.as_str(), new_words.as_str(), "correct horse"] {
        assert!(!all.contains(secret), "{secret:?} appears in formatted output:\n{all}");
    }
    assert!(all.contains("<redacted>"));

    // Nothing readable in what is stored either: header, tags, heads, bodies, key file.
    let store = f.vault.store();
    let mut stored: Vec<u8> = store.header.clone().unwrap();
    for (id, record) in &store.records {
        stored.extend_from_slice(id.as_bytes());
        stored.extend_from_slice(record.kind_tag.as_bytes());
        stored.extend_from_slice(record.index_tags.join(" ").as_bytes());
        stored.extend_from_slice(&record.head);
        stored.extend_from_slice(&record.body);
    }
    stored.extend_from_slice(&key_file);
    stored.extend_from_slice(&f.vault.export(NOW).unwrap());
    for secret in [canary, field, "memory", "topic", "correct horse"] {
        assert!(!stored.windows(secret.len()).any(|w| w == secret.as_bytes()), "{secret:?} is stored in the clear");
    }
}

// ---------------------------------------------------------------- passphrase keystore (the headless home)

#[test]
fn passphrase_keystore_for_the_headless_home() {
    let mut rng = TestRng::new(b"home");
    let (keystore, key_file) = PassphraseKeyStore::create("密碼 correct horse", Argon2Params::MINIMUM, &mut rng).unwrap();
    let (mut vault, _recovery) = Vault::create(MemoryStore::new(), "home", &keystore, &mut rng, NOW).unwrap();
    let id = vault.put("connector-token", b"tok", 1, &[("service", "mail")], &mut rng).unwrap();
    let store = vault.into_store();

    // After a restart: key file + passphrase + store.
    let reopened = PassphraseKeyStore::open("密碼 correct horse", &key_file).unwrap();
    let vault = Vault::open_with_device(store.clone(), &reopened).unwrap();
    assert_eq!(vault.get(&id).unwrap().payload.as_slice(), b"tok");
    // The same passphrase typed with a full-width space and full-width letters (NFKC).
    assert!(PassphraseKeyStore::open("密碼\u{3000}ｃｏｒｒｅｃｔ horse", &key_file).is_ok());

    assert!(PassphraseKeyStore::open("密碼 correct horsf", &key_file).is_err());
    assert!(PassphraseKeyStore::open("", &key_file).is_err());

    // The key file is authenticated, parameters included; weak parameters are refused outright.
    let json: serde_json::Value = serde_json::from_slice(&key_file).unwrap();
    assert_eq!((json["kdf"].as_str(), json["m_kib"].as_u64(), json["t"].as_u64(), json["p"].as_u64()), (Some("argon2id"), Some(19456), Some(2), Some(1)));
    let altered = |field: &str, value: serde_json::Value| {
        let mut json = json.clone();
        json[field] = value;
        PassphraseKeyStore::open("密碼 correct horse", &serde_json::to_vec(&json).unwrap()).is_err()
    };
    assert!(altered("m_kib", 8.into()), "below the minimum");
    assert!(altered("t", 1.into()), "below the minimum");
    assert!(altered("m_kib", (4 * 1024 * 1024).into()), "above the maximum");
    assert!(altered("t", 3.into()), "a different cost gives a different key");
    assert!(altered("version", 2.into()));
    assert!(altered("kdf", "scrypt".into()));
    assert!(altered("extra", 1.into()));
    for field in ["salt", "nonce", "wrapped"] {
        let mut text = json[field].as_str().unwrap().to_string();
        let last = text.pop().unwrap();
        text.push(if last == '0' { '1' } else { '0' });
        assert!(altered(field, text.into()), "{field}");
    }
    assert!(PassphraseKeyStore::create("pw", Argon2Params { m_kib: 1024, t: 1, p: 1 }, &mut rng).is_err());

    // A new passphrase for the same device key: the vault still opens.
    let new_file = reopened.key_file("a new passphrase", Argon2Params::MINIMUM, &mut rng).unwrap();
    let again = PassphraseKeyStore::open("a new passphrase", &new_file).unwrap();
    assert!(Vault::open_with_device(store, &again).is_ok());
    assert!(PassphraseKeyStore::open("密碼 correct horse", &new_file).is_err());
}

#[test]
fn default_argon2_parameters_work() {
    assert_eq!(Argon2Params::DEFAULT, Argon2Params { m_kib: 65536, t: 3, p: 1 });
    assert!(Argon2Params::DEFAULT.acceptable() && Argon2Params::MINIMUM.acceptable() && Argon2Params::MAXIMUM.acceptable());
    let mut rng = TestRng::new(b"default argon2");
    let (keystore, key_file) = PassphraseKeyStore::create("a passphrase", Argon2Params::DEFAULT, &mut rng).unwrap();
    let blob = keystore.wrap(&[7u8; 32]).unwrap();
    let reopened = PassphraseKeyStore::open("a passphrase", &key_file).unwrap();
    assert_eq!(reopened.unwrap(&blob).unwrap().as_slice(), &[7u8; 32]);
}

#[test]
fn keystore_blobs_are_authenticated() {
    let keystore = other_keystore("ks");
    let blob = keystore.wrap(b"thirty-two bytes of slot secret!").unwrap();
    assert_eq!(keystore.wrap(b"thirty-two bytes of slot secret!").unwrap(), blob, "deterministic");
    assert_eq!(keystore.unwrap(&blob).unwrap().as_slice(), b"thirty-two bytes of slot secret!");
    for at in 0..blob.len() {
        let mut altered = blob.clone();
        altered[at] ^= 1;
        assert!(keystore.unwrap(&altered).is_err(), "byte {at}");
    }
    assert!(keystore.unwrap(&blob[..10]).is_err());
    assert!(other_keystore("other").unwrap(&blob).is_err());
}

// ---------------------------------------------------------------- arguments and determinism

#[test]
fn arguments_are_checked() {
    let mut f = fixture("arguments");
    let invalid = |r: Result<String, VaultError>| matches!(r, Err(VaultError::Invalid(_)));
    for kind in ["", "Memory", "has space", "健康", &"k".repeat(65)] {
        assert!(invalid(f.vault.put(kind, b"x", 1, &[], &mut f.rng)), "{kind:?}");
    }
    assert!(invalid(f.vault.put("memory", b"x", 1, &[("Bad Name", "v")], &mut f.rng)));
    assert!(invalid(f.vault.put("memory", b"x", 1, &[("topic", &"v".repeat(1025))], &mut f.rng)));
    let many: Vec<(&str, &str)> = (0..17).map(|_| ("topic", "v")).collect();
    assert!(invalid(f.vault.put("memory", b"x", 1, &many, &mut f.rng)));
    assert!(f.vault.put("memory", b"x", 1, &many[..16], &mut f.rng).is_ok());
    assert!(f.vault.put("health.record-v2_a", b"", 0, &[], &mut f.rng).is_ok());

    let keystore = other_keystore("k");
    for label in ["", "a\nb", &"x".repeat(65)] {
        assert_eq!(
            Vault::create(MemoryStore::new(), label, &keystore, &mut f.rng, NOW).map(|_| ()).unwrap_err(),
            VaultError::Invalid("device label")
        );
    }
    assert_eq!(
        Vault::create(f.vault.store().clone(), "again", &keystore, &mut f.rng, NOW).map(|_| ()).unwrap_err(),
        VaultError::Exists
    );
    assert_eq!(Vault::open_with_device(MemoryStore::new(), &keystore).unwrap_err(), VaultError::NotFound);
    let mut garbage = MemoryStore::new();
    garbage.header = Some(b"not json".to_vec());
    assert_eq!(Vault::open_with_device(garbage, &keystore).unwrap_err(), VaultError::Malformed("header"));
}

#[test]
fn index_values_are_normalised() {
    for (typed, stored) in [
        ("  Tea ", "tea"), ("ＴＥＡ", "tea"), ("Ｃａｆé", "café"), ("cafe\u{301}", "café"), ("ΣΟΦΟΣ", "σοφος"),
        ("烏龍茶", "烏龍茶"), ("\u{3000}烏龍茶\u{3000}", "烏龍茶"), ("ﬁsh", "fish"), ("İ", "i\u{307}"), ("", ""),
    ] {
        assert_eq!(normalise_index_value(typed), stored, "{typed:?}");
    }
}

#[test]
fn keyed_references_are_stable_and_separate() {
    let f = fixture("refs");
    let a = f.vault.keyed_ref("record", b"abc").unwrap();
    assert_eq!(a, f.vault.keyed_ref("record", b"abc").unwrap());
    assert_ne!(a, f.vault.keyed_ref("record", b"abd").unwrap());
    assert_ne!(a, f.vault.keyed_ref("receipt", b"abc").unwrap());
    assert_ne!(f.vault.keyed_ref("ab", b"c").unwrap(), f.vault.keyed_ref("a", b"bc").unwrap(), "label and data cannot run together");
    assert_ne!(a, fixture("refs 2").vault.keyed_ref("record", b"abc").unwrap(), "another vault, another reference");
}

#[test]
fn the_same_random_stream_gives_the_same_bytes() {
    let run = || {
        let mut f = fixture("deterministic");
        f.vault.put("memory", b"m", 1, &[("topic", "tea")], &mut f.rng).unwrap();
        f.vault.rotate(RecoveryPolicy::Renew, &mut f.rng, NOW).unwrap();
        (f.vault.into_store(), f.recovery.words(Language::English).to_string())
    };
    assert_eq!(run(), run());
}

// ---------------------------------------------------------------- randomised (fixed seed)

#[test]
fn random_records_round_trip_through_everything() {
    let mut f = fixture("random round trips");
    confirm(&mut f);
    let mut gen = TestRng::new(b"generator");
    let kinds = ["memory", "token", "receipt", "mandate"];
    let mut expected: Vec<(String, &str, Vec<u8>, u32, String)> = Vec::new();
    for i in 0..120 {
        let size = match i % 6 {
            0 => 0,
            1 => number(&mut gen, 300),
            2 => 250 + number(&mut gen, 10),
            3 => number(&mut gen, 5_000),
            4 => 65_500 + number(&mut gen, 100),
            _ => number(&mut gen, 150_000),
        };
        let payload = bytes(&mut gen, size);
        let kind = kinds[number(&mut gen, kinds.len())];
        let schema = number(&mut gen, 1 << 20) as u32;
        let value = format!("v{}", number(&mut gen, 7));
        let id = f.vault.put(kind, &payload, schema, &[("group", &value)], &mut f.rng).unwrap();
        let stored = &f.vault.store().records[&id];
        assert_eq!(stored.body.len(), 44 + bucket_size(size + 4));
        expected.push((id, kind, payload, schema, value));
    }
    let check = |vault: &Vault<MemoryStore>| {
        for (id, kind, payload, schema, value) in &expected {
            let got = vault.get(id).unwrap();
            assert_eq!((got.kind.as_str(), got.payload.as_slice(), got.schema), (*kind, payload.as_slice(), *schema));
            assert!(vault.find(kind, "group", value).unwrap().contains(id));
        }
        for kind in kinds {
            let mut listed = vault.list(kind).unwrap();
            listed.sort();
            let mut wanted: Vec<String> = expected.iter().filter(|e| e.1 == kind).map(|e| e.0.clone()).collect();
            wanted.sort();
            assert_eq!(listed, wanted);
        }
    };
    check(&f.vault);
    f.vault.rotate(RecoveryPolicy::Keep, &mut f.rng, NOW).unwrap();
    check(&f.vault);
    let archive = f.vault.export(NOW).unwrap();
    let keys = other_keystore("restore");
    let restored = Vault::import(&archive, &f.recovery, MemoryStore::new(), "restored", &keys, &mut f.rng, NOW).unwrap();
    check(&restored);
    check(&Vault::open_with_recovery(f.vault.into_store(), &f.recovery).unwrap());
}

#[test]
fn random_damage_to_the_store_never_yields_wrong_content() {
    let mut f = fixture("random damage");
    let payload = b"exactly this content".to_vec();
    let ids: Vec<String> = (0..8).map(|_| f.vault.put("memory", &payload, 1, &[("topic", "tea")], &mut f.rng).unwrap()).collect();
    let clean = f.vault.into_store();
    let mut gen = TestRng::new(b"damage");
    for round in 0..400 {
        let mut store = clean.clone();
        let id = &ids[number(&mut gen, ids.len())];
        let record = store.records.get_mut(id).unwrap();
        let in_head = number(&mut gen, 2) == 0;
        let target = if in_head { &mut record.head } else { &mut record.body };
        match number(&mut gen, 4) {
            0 => {
                let at = number(&mut gen, target.len());
                target[at] ^= 1 << number(&mut gen, 8);
            }
            1 => target.truncate(number(&mut gen, target.len())),
            2 => {
                let extra = 1 + number(&mut gen, 40);
                target.extend(bytes(&mut gen, extra));
            }
            _ => {
                let other = clean.records[&ids[number(&mut gen, ids.len())]].clone();
                let replacement = if in_head { other.head } else { other.body };
                if replacement == *target {
                    continue;
                }
                *target = replacement;
            }
        }
        let vault = Vault::open_with_device(store, &f.keystore).unwrap();
        assert_eq!(vault.get(id).map(|r| r.payload.to_vec()).unwrap_err(), VaultError::Tampered, "round {round}");
        for other in ids.iter().filter(|other| *other != id) {
            assert_eq!(vault.get(other).unwrap().payload.as_slice(), payload.as_slice());
        }
    }
}

#[test]
fn stores_apply_batches_whole() {
    use muse_core::vault::StoreOp;
    let mut store = MemoryStore::new();
    let record = StoredRecord { kind_tag: "k".into(), index_tags: vec!["t".into()], head: vec![1], body: vec![2] };
    store.put("a", record.clone()).unwrap();
    // The second step fails (no such record), so the first must not be applied.
    let batch = vec![
        StoreOp::PutHeader(b"h".to_vec()),
        StoreOp::PutHead { id: "missing".into(), kind_tag: String::new(), index_tags: Vec::new(), head: Vec::new() },
    ];
    assert!(store.apply(batch).is_err());
    assert_eq!(store.header().unwrap(), None);
    store
        .apply(vec![
            StoreOp::PutHead { id: "a".into(), kind_tag: "k2".into(), index_tags: vec!["t2".into()], head: vec![9] },
            StoreOp::PutRecord { id: "b".into(), record: record.clone() },
        ])
        .unwrap();
    assert_eq!(store.get("a").unwrap().unwrap(), StoredRecord { kind_tag: "k2".into(), index_tags: vec!["t2".into()], head: vec![9], body: vec![2] });
    assert_eq!(store.list("k").unwrap(), vec!["b".to_string()]);
    assert_eq!(store.find("k2", "t2").unwrap(), vec!["a".to_string()]);
    assert_eq!(store.ids().unwrap(), vec!["a".to_string(), "b".to_string()]);
    assert_eq!(store.head("a").unwrap(), Some(vec![9]));
    store.delete("a").unwrap();
    assert_eq!(store.get("a").unwrap(), None);
}
