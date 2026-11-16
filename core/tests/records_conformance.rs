//! Replays `conformance/records.json` (written by `scripts/export_conformance.py`
//! from `muse/records/vectors.py`) against the Rust records code and requires
//! the same result: the same canonical bytes, hashes and signatures (Ed25519 is
//! deterministic), the same verdict and index for every broken chain, the same
//! Merkle roots and proofs.
//!
//! A vector is `{"name", "fn", "input", "expected"}`; `expected` is
//! `{"ok": value}` or `{"err": {"kind", "index"}}`. The seeds and reference
//! keys in the file are test-only. The Merkle vectors name a size `n`; their
//! record hashes are `SHA-256("muse records test leaf <i>")`.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use muse_core::records::{self, Checkpoint, Export, Hash, HmacKey, Kind, Problem, Record, SeedSigner};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FUNCTIONS: &[&str] = &[
    "canonicalize", "loads_strict", "key_shaped", "check_summary", "resource_ref", "summary_salt", "summary_hash", "genesis",
    "append", "canonical_bytes", "hash", "verify_record", "verify_signature", "verify_chain", "redact", "merkle_root",
    "inclusion_proof", "verify_inclusion", "consistency_proof", "verify_consistency", "checkpoint", "verify_checkpoint", "export",
    "export_text", "verify_export", "verify_export_text",
];

type Outcome = Result<Value, Problem>;

fn bad<T>() -> Result<T, Problem> {
    Err(Kind::Malformed.into())
}

fn text(v: &Value) -> Result<&str, Problem> {
    v.as_str().ok_or(Kind::Malformed.into())
}

/// A sequence number or size argument: Python refuses anything that is not a non-negative integer.
fn uint(v: &Value) -> Result<u64, Problem> {
    v.as_u64().ok_or(Kind::Malformed.into())
}

fn device(v: &Value) -> Option<&str> {
    v.as_str()
}

fn signer(v: &Value) -> SeedSigner {
    let seed: [u8; 32] = records::from_hex(v.as_str().expect("seed")).expect("seed hex").try_into().expect("32 bytes");
    SeedSigner::new(&seed)
}

fn ref_key(v: &Value) -> HmacKey {
    HmacKey::new(&records::from_hex(v.as_str().expect("key")).expect("key hex")).expect("key length")
}

fn test_leaves(n: &Value) -> Vec<Hash> {
    (0..n.as_u64().expect("n")).map(|i| Sha256::digest(format!("muse records test leaf {i}").as_bytes()).into()).collect()
}

fn hex_list(hashes: &[Hash]) -> Value {
    json!(hashes.iter().map(|h| records::to_hex(h)).collect::<Vec<_>>())
}

/// A proof argument as hashes; None when any element is not 64 lower-case hex (Python then answers False).
fn hashes_of(v: &Value) -> Option<Vec<Hash>> {
    v.as_array()?.iter().map(|h| records::hash_from_hex(h.as_str()?)).collect()
}

fn utf8(bytes: Vec<u8>) -> Outcome {
    Ok(Value::String(String::from_utf8(bytes).expect("canonical JSON is UTF-8")))
}

fn record_list(v: &Value) -> Result<Vec<Record>, Problem> {
    v.as_array().ok_or(Problem::from(Kind::Malformed))?.iter().map(Record::from_value).collect()
}

fn run(function: &str, i: &Value) -> Option<Outcome> {
    Some(match function {
        "canonicalize" => records::canonicalize(&i["value"]).and_then(utf8),
        "loads_strict" => text(&i["text"]).and_then(records::loads_strict).and_then(|v| records::canonicalize(&v)).and_then(utf8),
        "key_shaped" => Ok(json!(records::key_shaped(i["text"].as_str().expect("text")))),
        "check_summary" => match i["summary"].as_str() {
            Some(summary) => records::check_summary(summary).map(Value::String),
            None => Err(Kind::Summary.into()), // Python: a summary that is not text
        },
        "resource_ref" => text(&i["resource"]).and_then(|r| records::resource_ref(&ref_key(&i["test_only_ref_key"]), r)).map(Value::String),
        "summary_salt" => uint(&i["seq"])
            .and_then(|seq| records::summary_salt(&ref_key(&i["test_only_ref_key"]), text(&i["device"])?, seq))
            .map(Value::String),
        "summary_hash" => records::summary_hash(text(&i["salt"]).ok()?, text(&i["summary"]).ok()?).map(Value::String),
        "genesis" => records::genesis(text(&i["device"]).ok()?).map(Value::String),
        "append" => {
            let head = (!i["head"].is_null()).then_some(&i["head"]);
            records::append_value(head, &i["entry"], &signer(&i["test_only_seed"]), &ref_key(&i["test_only_ref_key"])).map(|r| r.to_value())
        }
        "canonical_bytes" => Record::from_value(&i["record"])
            .and_then(|r| records::canonical_bytes(&r, i["with_sig"].as_bool().expect("with_sig")))
            .and_then(utf8),
        "hash" => Record::from_value(&i["record"]).and_then(|r| records::hash(&r)).map(|h| json!(records::to_hex(&h))),
        "redact" => Record::from_value(&i["record"]).map(|r| records::redact(&r).to_value()),
        "verify_record" => records::verify_record_value(&i["record"], device(&i["device"])).map(|()| Value::Null),
        "verify_signature" => {
            let message = records::from_hex(i["message"].as_str().expect("message")).expect("message hex");
            Ok(json!(records::verify_signature(text(&i["device"]).ok()?, &message, text(&i["sig"]).ok()?)))
        }
        "verify_chain" => records::verify_chain_values(i["records"].as_array().expect("records"), device(&i["device"])).map(|n| json!(n)),
        "merkle_root" => Ok(json!(records::to_hex(&records::merkle_root(&test_leaves(&i["n"]))))),
        "inclusion_proof" => uint(&i["index"]).and_then(|index| records::inclusion_proof(&test_leaves(&i["n"]), index)).map(|p| hex_list(&p)),
        "consistency_proof" => {
            uint(&i["old_size"]).and_then(|old| records::consistency_proof(&test_leaves(&i["n"]), old)).map(|p| hex_list(&p))
        }
        "verify_inclusion" => {
            let parts = (|| {
                Some((records::hash_from_hex(i["hash"].as_str()?)?, i["index"].as_u64()?, i["size"].as_u64()?, hashes_of(&i["proof"])?, records::hash_from_hex(i["root"].as_str()?)?))
            })();
            Ok(json!(parts.is_some_and(|(hash, index, size, proof, root)| records::verify_inclusion(&hash, index, size, &proof, &root))))
        }
        "verify_consistency" => {
            let parts = (|| {
                Some((i["old_size"].as_u64()?, i["new_size"].as_u64()?, records::hash_from_hex(i["old_root"].as_str()?)?,
                      records::hash_from_hex(i["new_root"].as_str()?)?, hashes_of(&i["proof"])?))
            })();
            Ok(json!(parts.is_some_and(|(old, new, old_root, new_root, proof)| records::verify_consistency(old, new, &old_root, &new_root, &proof))))
        }
        "checkpoint" => uint(&i["time"])
            .and_then(|time| records::checkpoint(&test_leaves(&i["n"]), time, &signer(&i["test_only_seed"])))
            .map(|cp| cp.to_value()),
        "verify_checkpoint" => Checkpoint::from_value(&i["checkpoint"])
            .and_then(|cp| records::verify_checkpoint(&cp, device(&i["device"])))
            .map(|()| Value::Null),
        "export" => (|| {
            let cp = Checkpoint::from_value(&i["checkpoint"])?;
            let all = record_list(&i["records"])?;
            let select: Option<Vec<u64>> = match &i["select"] {
                Value::Null => None,
                // A negative number: Python reaches it after the size and root checks, as malformed. `u64::MAX` is refused the same way.
                Value::Array(items) => Some(items.iter().map(|s| s.as_u64().unwrap_or(u64::MAX)).collect()),
                _ => return bad(),
            };
            let redact = i["redact_summaries"].as_bool().expect("redact_summaries");
            Ok(records::export(&all, &cp, select.as_deref(), redact)?.to_value())
        })(),
        "export_text" => Export::from_value(&i["export"]).and_then(|e| records::export_text(&e)).map(Value::String),
        "verify_export" => records::verify_export_value(&i["export"], device(&i["device"])).map(summary_value),
        "verify_export_text" => records::verify_export_text(text(&i["text"]).ok()?, device(&i["device"])).map(summary_value),
        _ => return None,
    })
}

fn summary_value(s: records::ExportSummary) -> Value {
    json!({"device": s.device, "size": s.size, "count": s.count, "complete": s.complete, "redacted": s.redacted})
}

fn shown(outcome: Outcome) -> Value {
    match outcome {
        Ok(value) => json!({"ok": value}),
        Err(problem) => json!({"err": {"kind": problem.kind.as_str(), "index": problem.index}}),
    }
}

#[test]
fn every_records_vector_matches_python() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("conformance").join("records.json");
    let raw = fs::read_to_string(&path).expect("core/conformance/records.json");
    let vectors: Vec<Value> = serde_json::from_str(&raw).expect("records.json");
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut failures = Vec::new();
    for vector in &vectors {
        let (name, function) = (vector["name"].as_str().unwrap(), vector["fn"].as_str().unwrap());
        *counts.entry(function.to_string()).or_default() += 1;
        match run(function, &vector["input"]) {
            None => failures.push(format!("{name}: no Rust function for {function}")),
            Some(outcome) => {
                let got = shown(outcome);
                if got != vector["expected"] {
                    let input = vector["input"].to_string();
                    let input: String = input.chars().take(600).collect();
                    failures.push(format!("{name}\n  input    {input}\n  expected {}\n  got      {got}", vector["expected"]));
                }
            }
        }
    }
    for function in FUNCTIONS {
        assert!(counts.contains_key(*function), "records.json has no vectors for {function}");
    }
    assert_eq!(counts.len(), FUNCTIONS.len(), "records.json has vectors for functions this test does not expect: {counts:?}");
    println!("records.json: {} vectors {counts:?}", vectors.len());
    assert!(vectors.len() > 2000, "suspiciously few vectors: {}", vectors.len());
    if !failures.is_empty() {
        let shown: Vec<&str> = failures.iter().take(40).map(String::as_str).collect();
        panic!("{} of {} records vectors differ from Python:\n{}", failures.len(), vectors.len(), shown.join("\n"));
    }
}
