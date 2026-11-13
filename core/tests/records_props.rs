//! Properties of the records code on chains made here (the shared cases are in
//! `records_conformance.rs`): any single-bit change to a chain or an export is
//! detected, a proof verifies only for its own record, a redacted record still
//! verifies, and a store refuses anything but the next record.
//!
//! The seed and reference key below are test-only.

use muse_core::records::{
    self, Actor, ChainVerifier, Cost, Entry, Export, HmacKey, Kind, Links, MemoryStore, Outcome, Record, RecordStore, SeedSigner, Signer,
};
use serde_json::Value;

const T0: u64 = 1_794_000_000_000;

/// xorshift64*: enough for picking test cases, and the same on every run.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn signer() -> SeedSigner {
    SeedSigner::new(&[7u8; 32])
}

fn other_signer() -> SeedSigner {
    SeedSigner::new(&[8u8; 32])
}

fn ref_key() -> HmacKey {
    HmacKey::new(&[9u8; 32]).unwrap()
}

fn entry(rng: &mut Rng, time: u64, i: usize) -> Entry {
    let actions = ["files.write", "llm.call", "pay.spend", "mandate.issue", "card.answer", "home.pause"];
    let words = ["Wrote", "讀取", "Asked", "已允許", "Résumé", "Paused 👍"];
    Entry {
        time,
        actor: [Actor::Person, Actor::Helper, Actor::System][rng.below(3) as usize],
        action: actions[rng.below(6) as usize].to_string(),
        outcome: [Outcome::Ok, Outcome::Declined, Outcome::Failed, Outcome::Undone][rng.below(4) as usize],
        summary: format!("{} item {i} \"x\" \\ é中", words[rng.below(6) as usize]),
        mandate: (rng.below(2) == 0).then(|| format!("m-{}", rng.below(1000))),
        resource: (rng.below(3) != 0).then(|| format!("file:/docs/檔案-{i}.txt")),
        cost: (rng.below(4) == 0).then(|| Cost { currency: "TWD".to_string(), minor: rng.below(1 << 40) }),
        links: if rng.below(3) == 0 { Links { job: Some(format!("job-{}", rng.below(99))), ..Links::default() } } else { Links::default() },
        // From the index, not the generator, so the rest of each chain is what it was before `egress` existed.
        egress: [None, Some("none"), Some("home"), Some("service:gemini"), Some("host:openmausbot-tools")][i % 5].map(str::to_string),
    }
}

fn chain(seed: u64, n: usize) -> Vec<Record> {
    let mut rng = Rng(seed);
    let mut store = MemoryStore::new();
    let mut time = T0;
    for i in 0..n {
        time += rng.below(100_000);
        records::write(&mut store, &entry(&mut rng, time, i), &signer(), &ref_key()).unwrap();
    }
    store.records().to_vec()
}

fn kind<T: std::fmt::Debug>(result: Result<T, records::Problem>) -> (Kind, Option<u64>) {
    let problem = result.unwrap_err();
    (problem.kind, problem.index)
}

fn chain_text(records: &[Record]) -> Vec<u8> {
    records::canonicalize(&Value::Array(records.iter().map(Record::to_value).collect())).unwrap()
}

/// Read the text of a chain strictly and verify it; any failure is a detection.
fn accepts(raw: &[u8], device: &str) -> bool {
    let Ok(text) = std::str::from_utf8(raw) else { return false };
    let Ok(Value::Array(items)) = records::loads_strict(text) else { return false };
    records::verify_chain_values(&items, Some(device)).is_ok()
}

#[test]
fn random_chains_verify_and_round_trip_through_json() {
    for seed in 1..=8u64 {
        let records = chain(seed, 3 + (seed as usize * 7) % 30);
        let device = signer().device();
        assert_eq!(records::verify_chain(&records, Some(&device)), Ok(records.len() as u64));
        assert_eq!(records::verify_chain(&records, None), Ok(records.len() as u64));
        for (i, record) in records.iter().enumerate() {
            assert_eq!(record.seq, i as u64);
            assert_eq!(Record::from_value(&record.to_value()).as_ref(), Ok(record));
            assert_eq!(records::verify_record(record, Some(&device)), Ok(()));
        }
        assert!(accepts(&chain_text(&records), &device));
    }
}

#[test]
fn any_single_bit_change_to_a_chain_is_detected() {
    let device = signer().device();
    // Every bit of a short chain.
    let small = chain_text(&chain(21, 2));
    assert!(accepts(&small, &device));
    for bit in 0..small.len() * 8 {
        let mut changed = small.clone();
        changed[bit / 8] ^= 1 << (bit % 8);
        assert!(!accepts(&changed, &device), "bit {bit} of the short chain went unnoticed");
    }
    // A sample of the bits of longer ones.
    let mut rng = Rng(99);
    for seed in 30..34u64 {
        let raw = chain_text(&chain(seed, 12));
        for _ in 0..800 {
            let bit = rng.below(raw.len() as u64 * 8) as usize;
            let mut changed = raw.clone();
            changed[bit / 8] ^= 1 << (bit % 8);
            assert!(!accepts(&changed, &device), "bit {bit} of chain {seed} went unnoticed");
        }
    }
}

#[test]
fn removing_reordering_and_repeating_are_detected_at_the_right_index() {
    let records = chain(5, 9);
    for i in 0..8 {
        let mut removed = records.clone();
        removed.remove(i);
        assert_eq!(kind(records::verify_chain(&removed, None)), (Kind::Missing, Some(i as u64)));
        let mut swapped = records.clone();
        swapped.swap(i, i + 1);
        assert_eq!(kind(records::verify_chain(&swapped, None)), (Kind::Missing, Some(i as u64)));
        let mut twice = records.clone();
        twice.insert(i + 1, records[i].clone());
        assert_eq!(kind(records::verify_chain(&twice, None)), (Kind::Duplicate, Some(i as u64 + 1)));
    }
    // Cutting the tail leaves a valid, shorter chain: only a checkpoint can show it.
    assert_eq!(records::verify_chain(&records[..8], None), Ok(8));
}

#[test]
fn a_second_signed_record_for_one_seq_is_a_fork() {
    let records = chain(6, 5);
    let mut second = records[3].clone();
    second.time += 1;
    let second = records::sign(second, &signer()).unwrap();
    assert_eq!(records::verify_record(&second, None), Ok(()));
    let mut both = records[..4].to_vec();
    both.push(second.clone());
    assert_eq!(kind(records::verify_chain(&both, None)), (Kind::Fork, Some(4)));
    let mut replaced = records.clone();
    replaced[3] = second;
    assert_eq!(kind(records::verify_chain(&replaced, None)), (Kind::WrongPrev, Some(4)));
}

#[test]
fn another_device_cannot_extend_or_pose_as_the_chain() {
    let records = chain(7, 4);
    let mut foreign = records[2].clone();
    foreign.device = other_signer().device();
    let foreign = records::sign(foreign, &other_signer()).unwrap();
    let mut mixed = records.clone();
    mixed[2] = foreign;
    assert_eq!(kind(records::verify_chain(&mixed, None)), (Kind::WrongDevice, Some(2)));
    assert_eq!(kind(records::verify_chain(&records, Some(&other_signer().device()))), (Kind::WrongDevice, Some(0)));
    // Signing a record that names another device is refused outright.
    assert_eq!(kind(records::sign(records[0].clone(), &other_signer())), (Kind::WrongDevice, None));
    assert_eq!(kind(records::append(Some(&records[3]), &entry(&mut Rng(1), T0 * 2, 0), &other_signer(), &ref_key())), (Kind::WrongDevice, None));
}

#[test]
fn redacted_records_verify_and_keep_their_hash() {
    let records = chain(8, 10);
    let redacted: Vec<Record> = records.iter().map(records::redact).collect();
    assert!(redacted.iter().all(|r| r.summary.is_none() && r.summary_salt.is_none()));
    assert_eq!(records::verify_chain(&redacted, None), Ok(10));
    for (a, b) in records.iter().zip(&redacted) {
        assert_eq!(records::hash(a), records::hash(b));
        assert_eq!(records::canonical_bytes(a, true), records::canonical_bytes(b, true));
    }
    // A summary put back must be the signed one.
    let mut wrong = redacted[4].clone();
    wrong.summary = Some("Nothing happened".to_string());
    wrong.summary_salt = records[4].summary_salt.clone();
    assert_eq!(kind(records::verify_record(&wrong, None)), (Kind::SummaryMismatch, None));
}

#[test]
fn summaries_are_normalised_and_keys_are_refused() {
    let mut e = entry(&mut Rng(3), T0, 0);
    e.summary = "Cafe\u{301} re\u{301}sume\u{301}".to_string();
    let record = records::append(None, &e, &signer(), &ref_key()).unwrap();
    assert_eq!(record.summary.as_deref(), Some("Café résumé"));
    for bad in ["used sk-test0000000000000000", "a\nb", "", "\u{202e}abc", "-----BEGIN PRIVATE KEY-----"] {
        e.summary = bad.to_string();
        assert_eq!(kind(records::append(None, &e, &signer(), &ref_key())), (Kind::Summary, None), "{bad:?}");
    }
    e.summary = "x".repeat(201);
    assert_eq!(kind(records::append(None, &e, &signer(), &ref_key())), (Kind::Summary, None));
    // The resource never appears; only its keyed reference does.
    e.summary = "Wrote a file".to_string();
    e.resource = Some("file:/Notes/secret-plan.md".to_string());
    let record = records::append(None, &e, &signer(), &ref_key()).unwrap();
    assert!(!record.to_value().to_string().contains("secret-plan"));
    assert_eq!(record.resource, Some(records::resource_ref(&ref_key(), "file:/Notes/secret-plan.md").unwrap()));
    assert_ne!(record.resource, Some(records::resource_ref(&HmacKey::new(&[1u8; 32]).unwrap(), "file:/Notes/secret-plan.md").unwrap()));
}

#[test]
fn the_store_takes_only_the_next_record() {
    let records = chain(9, 4);
    let mut store = MemoryStore::new();
    assert_eq!(store.len(), Ok(0));
    assert_eq!(store.is_empty(), Ok(true));
    assert_eq!(store.head(), Ok(None));
    store.append(records[0].clone()).unwrap();
    assert_eq!(kind(store.append(records[0].clone())), (Kind::Fork, None));
    assert_eq!(kind(store.append(records[2].clone())), (Kind::Missing, None));
    let mut unlinked = records[1].clone();
    unlinked.prev = "00".repeat(32);
    let unlinked = records::sign(unlinked, &signer()).unwrap();
    assert_eq!(kind(store.append(unlinked)), (Kind::WrongPrev, None));
    let mut malformed = records[1].clone();
    malformed.sig = "zz".repeat(64);
    assert_eq!(kind(store.append(malformed)), (Kind::Malformed, None));
    store.append(records[1].clone()).unwrap();
    assert_eq!(store.len(), Ok(2));
    assert_eq!(store.get(1), Ok(Some(records[1].clone())));
    assert_eq!(store.get(2), Ok(None));
    assert_eq!(store.head(), Ok(Some(records[1].clone())));
    // A streaming verifier over the store agrees.
    let mut verifier = ChainVerifier::new(None);
    for record in store.records() {
        verifier.push(record).unwrap();
    }
    assert_eq!(verifier.size(), 2);
    assert_eq!(verifier.device(), Some(signer().device().as_str()));
}

#[test]
fn checkpoints_of_a_growing_chain_are_consistent() {
    let records = chain(10, 40);
    let hashes: Vec<_> = records.iter().map(|r| records::hash(r).unwrap()).collect();
    let mut previous: Option<records::Checkpoint> = None;
    for size in [0usize, 1, 2, 7, 8, 9, 31, 32, 33, 40] {
        let cp = records::checkpoint(&hashes[..size], T0 + size as u64, &signer()).unwrap();
        assert_eq!(records::verify_checkpoint(&cp, Some(&signer().device())), Ok(()));
        assert_eq!(kind(records::verify_checkpoint(&cp, Some(&other_signer().device()))), (Kind::WrongDevice, None));
        let mut edited = cp.clone();
        edited.size += 1;
        assert_eq!(kind(records::verify_checkpoint(&edited, None)), (Kind::BadSignature, None));
        if let Some(old) = &previous {
            let proof = records::consistency_proof(&hashes[..size], old.size).unwrap();
            let (old_root, new_root) = (records::hash_from_hex(&old.root).unwrap(), records::hash_from_hex(&cp.root).unwrap());
            assert!(records::verify_consistency(old.size, cp.size, &old_root, &new_root, &proof), "{} -> {}", old.size, cp.size);
        }
        previous = Some(cp);
    }
}

#[test]
fn exports_verify_alone_and_any_bit_change_is_detected() {
    let records = chain(11, 23);
    let device = signer().device();
    let hashes: Vec<_> = records.iter().map(|r| records::hash(r).unwrap()).collect();
    let cp = records::checkpoint(&hashes, T0, &signer()).unwrap();
    let mut rng = Rng(4);
    let full = records::export(&records, &cp, None, false).unwrap();
    let summary = records::verify_export(&full, Some(&device)).unwrap();
    assert_eq!((summary.size, summary.count, summary.complete, summary.redacted), (23, 23, true, 0));
    for round in 0..12 {
        let mut select: Vec<u64> = (0..23).filter(|_| rng.below(3) == 0).collect();
        select.dedup();
        let redact = round % 2 == 0;
        let export = records::export(&records, &cp, Some(&select), redact).unwrap();
        let text = records::export_text(&export).unwrap();
        let summary = records::verify_export_text(&text, None).unwrap();
        assert_eq!((summary.count, summary.complete), (select.len() as u64, false));
        assert_eq!(summary.redacted, if redact { select.len() as u64 } else { 0 });
        assert_eq!(Export::from_value(&records::loads_strict(&text).unwrap()), Ok(export.clone()));
        // A record that was not selected cannot ride on another record's proof.
        if let (Some(first), Some(other)) = (select.first(), (0..23u64).find(|s| !select.contains(s))) {
            let mut swapped = export.clone();
            swapped.records[0] = records[other as usize].clone();
            let (k, index) = kind(records::verify_export(&swapped, None));
            assert!(matches!(k, Kind::BadProof | Kind::Malformed), "{first} replaced by {other}: {k:?} at {index:?}");
        }
        let raw = text.as_bytes();
        for _ in 0..250 {
            let bit = rng.below((raw.len() as u64 - 1) * 8) as usize; // the final newline is not data
            let mut changed = raw.to_vec();
            changed[bit / 8] ^= 1 << (bit % 8);
            let verdict = std::str::from_utf8(&changed).ok().map(|t| records::verify_export_text(t, None));
            assert!(!matches!(verdict, Some(Ok(_))), "bit {bit} of an export went unnoticed");
        }
    }
    // An export made by another key is valid on its own and is not this device's.
    assert_eq!(kind(records::verify_export(&full, Some(&other_signer().device()))), (Kind::WrongDevice, None));
}

#[test]
fn no_float_reaches_a_record() {
    let record = chain(12, 1).remove(0);
    let mut value = record.to_value();
    value["time"] = serde_json::json!(record.time as f64);
    assert_eq!(kind(Record::from_value(&value)), (Kind::Malformed, None));
    value["time"] = serde_json::json!(record.time);
    value["cost"] = serde_json::json!({"currency": "TWD", "minor": 0.5});
    assert_eq!(kind(Record::from_value(&value)), (Kind::Malformed, None));
    assert!(records::canonicalize(&serde_json::json!({"a": 1.5})).is_err());
    let mut too_big = record.clone();
    too_big.seq = records::MAX_INT + 1;
    assert_eq!(kind(records::canonical_bytes(&too_big, false)), (Kind::Malformed, None));
}

#[test]
fn egress_is_signed_optional_and_says_what_left() {
    let records = chain(21, 10); // index i carries: nothing, none, home, service:gemini, host:..., and again
    let device = signer().device();
    let said: Vec<Option<&str>> = records.iter().map(|r| r.egress.as_deref()).collect();
    assert_eq!(&said[..5], &[None, Some("none"), Some("home"), Some("service:gemini"), Some("host:openmausbot-tools")]);
    let left: Vec<bool> = records.iter().map(records::left_devices).collect();
    assert_eq!(&left[..5], &[false, false, false, true, true]); // "does not say" is not "left"
    for record in &records {
        records::verify_record(record, Some(&device)).unwrap();
        let bytes = String::from_utf8(records::canonical_bytes(record, false).unwrap()).unwrap();
        assert_eq!(bytes.contains("\"egress\""), record.egress.is_some()); // absent is left out, never null
        assert_eq!(Record::from_value(&record.to_value()).unwrap(), *record);
        // Changing, adding or removing it breaks the signature.
        let mut edited = record.clone();
        edited.egress = if record.egress.as_deref() == Some("none") { None } else { Some("none".to_string()) };
        assert_eq!(kind(records::verify_record(&edited, Some(&device))), (Kind::BadSignature, None));
        // Redaction keeps it.
        assert_eq!(records::redact(record).egress, record.egress);
    }
    for bad in ["", "cloud", "service:", "host:", "service:Gemini", "None", "service:a b"] {
        let mut value = records[3].to_value();
        value["egress"] = serde_json::json!(bad);
        assert_eq!(kind(Record::from_value(&value)), (Kind::Malformed, None), "{bad:?}");
    }
    let mut value = records[3].to_value();
    value["egress"] = Value::Null;
    assert_eq!(kind(Record::from_value(&value)), (Kind::Malformed, None));
}
