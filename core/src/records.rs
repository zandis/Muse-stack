//! Records: the signed, hash-chained entries of one device (format v1).
//!
//! The format is specified in `docs/spec/interfaces/record-format.md`. This is
//! its Rust implementation; `muse/records/` is the Python one, and both are
//! held to `core/conformance/records.json` (replayed by
//! `tests/records_conformance.rs`). Everything here is pure: no clock, no
//! files, no randomness (Ed25519 signatures are deterministic). Storage is the
//! [`RecordStore`] trait; the vault supplies the real one.
//!
//! - [`append`] makes the signed record that follows a head; [`sign`] signs one.
//! - [`verify_record`], [`verify_chain`] and [`ChainVerifier`] check them.
//! - [`merkle`]: roots, inclusion proofs, consistency proofs (RFC 6962).
//! - [`checkpoint`] / [`verify_checkpoint`]: the signed root ("sealed").
//! - [`export`] / [`verify_export`]: a file a third party can check alone;
//!   [`redact`] removes a summary and leaves the record verifiable.

use ed25519_dalek::{Signature, Signer as _, SigningKey, Verifier as _, VerifyingKey};
use hmac::{Hmac, Mac};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

pub mod jcs;
pub mod merkle;
pub mod store;

pub use jcs::{canonicalize, loads_strict, MAX_INT};
pub use merkle::{consistency_proof, inclusion_proof, leaf_hash, merkle_root, verify_consistency, verify_inclusion};
pub use store::{check_extends, write, MemoryStore, RecordStore};

pub const FORMAT_VERSION: u64 = 1;
/// A record may be this much earlier than the one before it (milliseconds).
pub const TIME_TOLERANCE_MS: u64 = 120_000;
pub const MAX_SUMMARY_CHARS: usize = 200;
pub const MAX_RESOURCE_CHARS: usize = 1024;

pub const EXPORT_TYPE: &str = "muse.records.export";
pub const EXPORT_FORMAT: u64 = 1;
pub const SPEC: &str = "record-format/1";

const D_SIG: &[u8] = b"muse/record/v1/sig\x00";
const D_HASH: &[u8] = b"muse/record/v1/hash\x00";
const D_GENESIS: &[u8] = b"muse/record/v1/genesis\x00";
const D_RESOURCE: &[u8] = b"muse/record/v1/resource\x00";
const D_SALT: &[u8] = b"muse/record/v1/summary-salt\x00";
const D_SUMMARY: &[u8] = b"muse/record/v1/summary\x00";
const D_CHECKPOINT: &[u8] = b"muse/checkpoint/v1/sig\x00";

const REQUIRED_FIELDS: [&str; 13] =
    ["v", "device", "seq", "time", "mandate", "actor", "action", "resource", "summary_hash", "outcome", "cost", "prev", "sig"];
const OPTIONAL_FIELDS: [&str; 3] = ["links", "summary", "summary_salt"];
const ENTRY_REQUIRED: [&str; 5] = ["time", "actor", "action", "outcome", "summary"];
const ENTRY_OPTIONAL: [&str; 4] = ["mandate", "resource", "cost", "links"];

/// Summary rule, part 3 (append only): prefixes of well-known credentials.
pub const KEY_PREFIXES: [&str; 25] = [
    "sk-", "sk_", "pk_", "rk_", "gsk_", "csk-", "nvapi-", "jina_", "hf_", "glpat-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
    "github_pat_", "xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-", "AKIA", "ASIA", "AIza", "eyJ",
];
const KEY_PREFIX_MIN: usize = 20;
const KEY_RUN_MIN: usize = 32;

/// A SHA-256 value.
pub type Hash = [u8; 32];

// ---------------------------------------------------------------- problems

/// Every way a record, chain, checkpoint or export can be wrong. The names
/// (`as_str`) are the Python `RecordError.kind` values.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    /// Not the shape or value range the format allows.
    Malformed,
    /// Append only: the summary breaks the summary rule.
    Summary,
    /// Signed by, or naming, another device than the chain's.
    WrongDevice,
    /// The signature does not verify.
    BadSignature,
    /// A summary is present but is not the one the record commits to.
    SummaryMismatch,
    /// A sequence number was skipped: a record was removed, or the order changed.
    Missing,
    /// The same record appears twice.
    Duplicate,
    /// Two different validly signed records claim one sequence number.
    Fork,
    /// A validly signed record does not link to the record before it.
    WrongPrev,
    /// Time is earlier than the previous record's by more than the tolerance.
    TimeWentBack,
    /// The checkpoint is malformed or its signature does not verify.
    BadCheckpoint,
    /// A complete export does not hold exactly `checkpoint.size` records.
    SizeMismatch,
    /// The records do not hash to the checkpoint's root.
    RootMismatch,
    /// An inclusion proof does not lead to the checkpoint's root.
    BadProof,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Malformed => "malformed",
            Kind::Summary => "summary",
            Kind::WrongDevice => "wrong_device",
            Kind::BadSignature => "bad_signature",
            Kind::SummaryMismatch => "summary_mismatch",
            Kind::Missing => "missing",
            Kind::Duplicate => "duplicate",
            Kind::Fork => "fork",
            Kind::WrongPrev => "wrong_prev",
            Kind::TimeWentBack => "time_went_back",
            Kind::BadCheckpoint => "bad_checkpoint",
            Kind::SizeMismatch => "size_mismatch",
            Kind::RootMismatch => "root_mismatch",
            Kind::BadProof => "bad_proof",
        }
    }
}

/// What is wrong, and the position of the first bad item when there is one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Problem {
    pub kind: Kind,
    pub index: Option<u64>,
}

impl Problem {
    fn at(self, index: u64) -> Problem {
        Problem { kind: self.kind, index: Some(index) }
    }
}

impl From<Kind> for Problem {
    fn from(kind: Kind) -> Problem {
        Problem { kind, index: None }
    }
}

impl std::fmt::Display for Problem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.index {
            Some(i) => write!(f, "{} at {i}", self.kind.as_str()),
            None => f.write_str(self.kind.as_str()),
        }
    }
}

impl std::error::Error for Problem {}

fn malformed<T>() -> Result<T, Problem> {
    Err(Kind::Malformed.into())
}

// ---------------------------------------------------------------- hex and hashing

/// Lower-case hex.
pub fn to_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 15) as usize] as char);
    }
    out
}

/// The bytes of lower-case hex; None for anything else (upper case included).
pub fn from_hex(text: &str) -> Option<Vec<u8>> {
    let raw = text.as_bytes();
    if !raw.len().is_multiple_of(2) {
        return None;
    }
    let digit = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    };
    raw.chunks(2).map(|pair| Some(digit(pair[0])? << 4 | digit(pair[1])?)).collect()
}

/// A hash from its 64 lower-case hex characters.
pub fn hash_from_hex(text: &str) -> Option<Hash> {
    from_hex(text)?.try_into().ok()
}

fn is_hex(text: &str, chars: usize) -> bool {
    text.len() == chars && text.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn sha256(parts: &[&[u8]]) -> Hash {
    let mut h = Sha256::new();
    for part in parts {
        h.update(part);
    }
    h.finalize().into()
}

// ---------------------------------------------------------------- keys

/// What signs for a device. `SeedSigner` holds a key in memory; the vault implements this later.
pub trait Signer {
    /// The device id: lower-case hex of the 32-byte Ed25519 public key.
    fn device(&self) -> String;
    /// The 64-byte Ed25519 signature of `message`.
    fn sign(&self, message: &[u8]) -> [u8; 64];
}

/// The per-vault reference key, used only through HMAC-SHA-256.
pub trait RefKey {
    fn mac(&self, message: &[u8]) -> Hash;
}

/// A [`Signer`] from a 32-byte Ed25519 seed held in memory.
pub struct SeedSigner {
    key: SigningKey,
}

impl SeedSigner {
    pub fn new(seed: &[u8; 32]) -> SeedSigner {
        SeedSigner { key: SigningKey::from_bytes(seed) }
    }
}

impl Signer for SeedSigner {
    fn device(&self) -> String {
        to_hex(self.key.verifying_key().as_bytes())
    }

    fn sign(&self, message: &[u8]) -> [u8; 64] {
        self.key.sign(message).to_bytes()
    }
}

/// A [`RefKey`] from key bytes held in memory (at least 16).
pub struct HmacKey {
    key: Vec<u8>,
}

impl HmacKey {
    pub fn new(key: &[u8]) -> Result<HmacKey, Problem> {
        if key.len() < 16 {
            return malformed();
        }
        Ok(HmacKey { key: key.to_vec() })
    }
}

impl RefKey for HmacKey {
    fn mac(&self, message: &[u8]) -> Hash {
        // HMAC accepts a key of any length, so this cannot fail.
        let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&self.key).expect("HMAC takes any key length");
        mac.update(message);
        mac.finalize().into_bytes().into()
    }
}

/// Does `sig` (128 hex) verify for `message` under the device key `device` (64 hex)?
///
/// Ed25519 as in RFC 8032 section 5.1.7 without the cofactor: S must be below
/// the group order, R is compared as encoded bytes, and keys of small order are
/// not refused (a device that publishes one harms only itself). This is what
/// OpenSSL, behind the Python twin, does; the conformance vectors hold the edge cases.
pub fn verify_signature(device: &str, message: &[u8], sig: &str) -> bool {
    let (Some(key), Some(sig)) = (from_hex(device), from_hex(sig)) else {
        return false;
    };
    let (Ok(key), Ok(sig)): (Result<[u8; 32], _>, Result<[u8; 64], _>) = (key.try_into(), sig.try_into()) else {
        return false;
    };
    match VerifyingKey::from_bytes(&key) {
        Ok(key) => key.verify(message, &Signature::from_bytes(&sig)).is_ok(),
        Err(_) => false,
    }
}

// ---------------------------------------------------------------- text rules

fn is_id(text: &str) -> bool {
    (1..=128).contains(&text.len()) && text.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b':' | b'-'))
}

fn is_action(text: &str) -> bool {
    let segments: Vec<&str> = text.split('.').collect();
    (2..=4).contains(&segments.len())
        && segments.iter().all(|s| {
            let b = s.as_bytes();
            (1..=32).contains(&b.len())
                && b[0].is_ascii_lowercase()
                && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
        })
}

fn is_currency(text: &str) -> bool {
    (3..=8).contains(&text.len()) && text.bytes().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
}

fn bad_char(c: char) -> bool {
    let code = c as u32;
    code < 0x20 || (0x7f..=0x9f).contains(&code) || code == 0x2028 || code == 0x2029 || (0x202a..=0x202e).contains(&code) || (0x2066..=0x2069).contains(&code)
}

/// Parts 1 and 2 of the summary rule: what every reader checks.
fn summary_shape_ok(summary: &str) -> bool {
    let mut count = 0;
    for c in summary.chars() {
        count += 1;
        if count > MAX_SUMMARY_CHARS || bad_char(c) {
            return false;
        }
    }
    count >= 1
}

/// Part 3 of the summary rule: does `text` hold something shaped like a credential?
///
/// A token is a longest run of ASCII letters, digits and `_ + = -`. The text is
/// key-shaped when it contains `-----BEGIN`, or a token of 20 or more characters
/// that starts with a known key prefix, or a token with a piece (between
/// hyphens) of 32 or more characters holding both a letter and a digit.
pub fn key_shaped(text: &str) -> bool {
    if text.contains("-----BEGIN") {
        return true;
    }
    let token_char = |c: char| c.is_ascii_alphanumeric() || matches!(c, '_' | '+' | '=' | '-');
    text.split(|c| !token_char(c)).any(|token| {
        (token.len() >= KEY_PREFIX_MIN && KEY_PREFIXES.iter().any(|p| token.starts_with(p)))
            || token.split('-').any(|piece| {
                piece.len() >= KEY_RUN_MIN && piece.bytes().any(|c| c.is_ascii_digit()) && piece.bytes().any(|c| c.is_ascii_alphabetic())
            })
    })
}

/// The summary as it will be signed (NFC), or `Summary` when it breaks the rule:
/// 1 to 200 characters after normalisation, no control or bidi-override
/// characters, nothing key-shaped.
pub fn check_summary(summary: &str) -> Result<String, Problem> {
    let text: String = summary.nfc().collect();
    if !summary_shape_ok(&text) || key_shaped(&text) {
        return Err(Kind::Summary.into());
    }
    Ok(text)
}

// ---------------------------------------------------------------- references and commitments

/// The keyed reference stored in `resource`: HMAC-SHA-256 over the NFC resource string.
pub fn resource_ref(ref_key: &dyn RefKey, resource: &str) -> Result<String, Problem> {
    if !(1..=MAX_RESOURCE_CHARS).contains(&resource.chars().count()) {
        return malformed();
    }
    let text: String = resource.nfc().collect();
    Ok(to_hex(&ref_key.mac(&[D_RESOURCE, text.as_bytes()].concat())))
}

/// The 16-byte salt of a record's summary commitment, derived so that `append` needs no randomness.
pub fn summary_salt(ref_key: &dyn RefKey, device: &str, seq: u64) -> Result<String, Problem> {
    if !is_hex(device, 64) || seq > MAX_INT {
        return malformed();
    }
    let mac = ref_key.mac(&[D_SALT, device.as_bytes(), b"\x00", seq.to_string().as_bytes()].concat());
    Ok(to_hex(&mac[..16]))
}

/// What a record signs in place of its summary: SHA-256(domain || salt || UTF-8 summary).
pub fn summary_hash(salt: &str, summary: &str) -> Result<String, Problem> {
    if !is_hex(salt, 32) {
        return malformed();
    }
    let salt = from_hex(salt).ok_or(Problem::from(Kind::Malformed))?;
    Ok(to_hex(&sha256(&[D_SUMMARY, &salt, summary.as_bytes()])))
}

/// `prev` of a device's first record (seq 0).
pub fn genesis(device: &str) -> Result<String, Problem> {
    let key = hash_from_hex(device).ok_or(Problem::from(Kind::Malformed))?;
    Ok(to_hex(&sha256(&[D_GENESIS, &key])))
}

// ---------------------------------------------------------------- types

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Actor {
    Person,
    Helper,
    System,
}

impl Actor {
    pub fn as_str(self) -> &'static str {
        match self {
            Actor::Person => "person",
            Actor::Helper => "helper",
            Actor::System => "system",
        }
    }

    fn parse(v: &Value) -> Result<Actor, Problem> {
        match v.as_str() {
            Some("person") => Ok(Actor::Person),
            Some("helper") => Ok(Actor::Helper),
            Some("system") => Ok(Actor::System),
            _ => malformed(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Outcome {
    Ok,
    Declined,
    Failed,
    Undone,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Ok => "ok",
            Outcome::Declined => "declined",
            Outcome::Failed => "failed",
            Outcome::Undone => "undone",
        }
    }

    fn parse(v: &Value) -> Result<Outcome, Problem> {
        match v.as_str() {
            Some("ok") => Ok(Outcome::Ok),
            Some("declined") => Ok(Outcome::Declined),
            Some("failed") => Ok(Outcome::Failed),
            Some("undone") => Ok(Outcome::Undone),
            _ => malformed(),
        }
    }
}

/// An amount: a currency code and an integer count of its minor unit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cost {
    pub currency: String,
    pub minor: u64,
}

impl Cost {
    fn check(&self) -> Result<(), Problem> {
        if !is_currency(&self.currency) || self.minor > MAX_INT {
            return malformed();
        }
        Ok(())
    }

    fn parse(v: &Value) -> Result<Option<Cost>, Problem> {
        if v.is_null() {
            return Ok(None);
        }
        let Some(map) = v.as_object() else {
            return malformed();
        };
        match (map.len(), map.get("currency").and_then(Value::as_str), map.get("minor").and_then(Value::as_u64)) {
            (2, Some(currency), Some(minor)) => {
                let cost = Cost { currency: currency.to_string(), minor };
                cost.check()?;
                Ok(Some(cost))
            }
            _ => malformed(),
        }
    }

    fn to_value(&self) -> Value {
        json!({"currency": self.currency, "minor": self.minor})
    }
}

/// Optional pointers from a record to other things. All `None` means the record has no `links`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Links {
    pub job: Option<String>,
    pub card: Option<String>,
    /// The hash (64 hex) of the record this one undoes.
    pub undo_of: Option<String>,
    pub payment_ref: Option<String>,
}

impl Links {
    pub fn is_empty(&self) -> bool {
        self.job.is_none() && self.card.is_none() && self.undo_of.is_none() && self.payment_ref.is_none()
    }

    fn check(&self) -> Result<(), Problem> {
        let ids_ok = [&self.job, &self.card, &self.payment_ref].into_iter().flatten().all(|s| is_id(s));
        if !ids_ok || self.undo_of.as_deref().is_some_and(|s| !is_hex(s, 64)) {
            return malformed();
        }
        Ok(())
    }

    /// `links` as it appears in JSON: absent (None) or a non-empty object of known keys with text values.
    fn parse(v: Option<&Value>) -> Result<Links, Problem> {
        let Some(v) = v else {
            return Ok(Links::default());
        };
        let Some(map) = v.as_object() else {
            return malformed();
        };
        if map.is_empty() {
            return malformed();
        }
        let mut links = Links::default();
        for (key, value) in map {
            let Some(text) = value.as_str() else {
                return malformed();
            };
            let slot = match key.as_str() {
                "job" => &mut links.job,
                "card" => &mut links.card,
                "undo_of" => &mut links.undo_of,
                "payment_ref" => &mut links.payment_ref,
                _ => return malformed(),
            };
            *slot = Some(text.to_string());
        }
        links.check()?;
        Ok(links)
    }

    fn to_value(&self) -> Value {
        let mut map = Map::new();
        for (key, value) in [("job", &self.job), ("card", &self.card), ("undo_of", &self.undo_of), ("payment_ref", &self.payment_ref)] {
            if let Some(text) = value {
                map.insert(key.to_string(), Value::String(text.clone()));
            }
        }
        Value::Object(map)
    }
}

fn keys_ok(map: &Map<String, Value>, required: &[&str], optional: &[&str]) -> bool {
    required.iter().all(|k| map.contains_key(*k)) && map.keys().all(|k| required.contains(&k.as_str()) || optional.contains(&k.as_str()))
}

fn opt_text(v: Option<&Value>) -> Result<Option<String>, Problem> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => malformed(),
    }
}

fn text(v: &Value) -> Result<String, Problem> {
    v.as_str().map(str::to_string).ok_or(Problem::from(Kind::Malformed))
}

/// A non-negative integer in range; a float (even `1.0`) is not one.
fn int(v: &Value) -> Result<u64, Problem> {
    v.as_u64().filter(|n| *n <= MAX_INT).ok_or(Problem::from(Kind::Malformed))
}

/// What the caller hands to [`append`]: everything about the action except its place in the chain.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// Milliseconds since 1970-01-01T00:00:00Z.
    pub time: u64,
    pub actor: Actor,
    /// Dotted lower-case name, e.g. `files.write`.
    pub action: String,
    pub outcome: Outcome,
    /// Short human text that is safe to show (see [`check_summary`]).
    pub summary: String,
    pub mandate: Option<String>,
    /// The plain canonical resource string; only its keyed reference is stored.
    pub resource: Option<String>,
    pub cost: Option<Cost>,
    pub links: Links,
}

impl Entry {
    /// An entry from its JSON object. `Malformed` for a wrong shape; `Summary` when the summary is not text.
    pub fn from_value(v: &Value) -> Result<Entry, Problem> {
        let Some(map) = v.as_object() else {
            return malformed();
        };
        if !keys_ok(map, &ENTRY_REQUIRED, &ENTRY_OPTIONAL) {
            return malformed();
        }
        let entry = Entry {
            time: int(&map["time"])?,
            actor: Actor::parse(&map["actor"])?,
            action: text(&map["action"])?,
            outcome: Outcome::parse(&map["outcome"])?,
            summary: String::new(),
            mandate: opt_text(map.get("mandate"))?,
            resource: None,
            cost: Cost::parse(map.get("cost").unwrap_or(&Value::Null))?,
            links: Links::parse(map.get("links"))?,
        };
        entry.check()?;
        let summary = map["summary"].as_str().ok_or(Problem::from(Kind::Summary))?.to_string();
        let resource = match map.get("resource") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            // Not text: reported where Python reports it, after the summary and the head.
            Some(_) => Some(String::new()),
        };
        Ok(Entry { summary, resource, ..entry })
    }

    /// The fields an entry and a record share.
    fn check(&self) -> Result<(), Problem> {
        if self.time > MAX_INT || !is_action(&self.action) || self.mandate.as_deref().is_some_and(|m| !is_id(m)) {
            return malformed();
        }
        if let Some(cost) = &self.cost {
            cost.check()?;
        }
        self.links.check()
    }
}

/// One signed record. Field for field the JSON object of the format (`v` is always 1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    /// The signing device: lower-case hex of its Ed25519 public key.
    pub device: String,
    /// Position in the device's chain, from 0.
    pub seq: u64,
    /// Milliseconds since 1970-01-01T00:00:00Z.
    pub time: u64,
    /// The permission it ran under; None for the person's own direct action.
    pub mandate: Option<String>,
    pub actor: Actor,
    pub action: String,
    /// The keyed reference of the resource (64 hex), never the resource itself.
    pub resource: Option<String>,
    /// The commitment to the summary; this, not the summary, is signed.
    pub summary_hash: String,
    pub outcome: Outcome,
    pub cost: Option<Cost>,
    pub links: Links,
    /// The hash of the record before, or the device's genesis value.
    pub prev: String,
    /// Ed25519 signature, 128 hex.
    pub sig: String,
    /// The summary and its salt: both present, or both absent (redacted).
    pub summary: Option<String>,
    pub summary_salt: Option<String>,
}

impl Record {
    /// A record from its JSON object, strictly: unknown or missing fields, wrong types and out-of-range values are `Malformed`.
    pub fn from_value(v: &Value) -> Result<Record, Problem> {
        let Some(map) = v.as_object() else {
            return malformed();
        };
        if !keys_ok(map, &REQUIRED_FIELDS, &OPTIONAL_FIELDS) || map["v"].as_u64() != Some(FORMAT_VERSION) {
            return malformed();
        }
        let optional_text = |key: &str| match map.get(key) {
            None => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(_) => malformed(),
        };
        let record = Record {
            device: text(&map["device"])?,
            seq: int(&map["seq"])?,
            time: int(&map["time"])?,
            mandate: opt_text(map.get("mandate"))?,
            actor: Actor::parse(&map["actor"])?,
            action: text(&map["action"])?,
            resource: opt_text(map.get("resource"))?,
            summary_hash: text(&map["summary_hash"])?,
            outcome: Outcome::parse(&map["outcome"])?,
            cost: Cost::parse(&map["cost"])?,
            links: Links::parse(map.get("links"))?,
            prev: text(&map["prev"])?,
            sig: text(&map["sig"])?,
            summary: optional_text("summary")?,
            summary_salt: optional_text("summary_salt")?,
        };
        record.check()?;
        Ok(record)
    }

    /// `Malformed` unless every field is in the range the format allows. Shape only:
    /// it says nothing about the signature or the chain.
    pub fn check(&self) -> Result<(), Problem> {
        let ok = is_hex(&self.device, 64)
            && self.seq <= MAX_INT
            && self.time <= MAX_INT
            && self.mandate.as_deref().is_none_or(is_id)
            && is_action(&self.action)
            && self.resource.as_deref().is_none_or(|r| is_hex(r, 64))
            && is_hex(&self.summary_hash, 64)
            && is_hex(&self.prev, 64)
            && is_hex(&self.sig, 128)
            && self.summary.is_some() == self.summary_salt.is_some()
            && self.summary.as_deref().is_none_or(summary_shape_ok)
            && self.summary_salt.as_deref().is_none_or(|s| is_hex(s, 32));
        if !ok {
            return malformed();
        }
        if let Some(cost) = &self.cost {
            cost.check()?;
        }
        self.links.check()
    }

    fn signed_value(&self, with_sig: bool) -> Value {
        let mut map = Map::new();
        map.insert("v".into(), json!(FORMAT_VERSION));
        map.insert("device".into(), json!(self.device));
        map.insert("seq".into(), json!(self.seq));
        map.insert("time".into(), json!(self.time));
        map.insert("mandate".into(), json!(self.mandate));
        map.insert("actor".into(), json!(self.actor.as_str()));
        map.insert("action".into(), json!(self.action));
        map.insert("resource".into(), json!(self.resource));
        map.insert("summary_hash".into(), json!(self.summary_hash));
        map.insert("outcome".into(), json!(self.outcome.as_str()));
        map.insert("cost".into(), self.cost.as_ref().map_or(Value::Null, Cost::to_value));
        map.insert("prev".into(), json!(self.prev));
        if !self.links.is_empty() {
            map.insert("links".into(), self.links.to_value());
        }
        if with_sig {
            map.insert("sig".into(), json!(self.sig));
        }
        Value::Object(map)
    }

    /// The record as its JSON object.
    pub fn to_value(&self) -> Value {
        let mut value = self.signed_value(true);
        if let (Some(map), Some(summary), Some(salt)) = (value.as_object_mut(), &self.summary, &self.summary_salt) {
            map.insert("summary".into(), json!(summary));
            map.insert("summary_salt".into(), json!(salt));
        }
        value
    }
}

// ---------------------------------------------------------------- bytes, hash, signature

/// The canonical (RFC 8785) bytes of a record's signed fields; with `sig` too when `with_sig`.
/// `summary` and `summary_salt` are never part of them: the record signs `summary_hash`.
pub fn canonical_bytes(record: &Record, with_sig: bool) -> Result<Vec<u8>, Problem> {
    canonicalize(&record.signed_value(with_sig))
}

/// The 32 bytes Ed25519 signs.
pub fn signing_digest(record: &Record) -> Result<Hash, Problem> {
    Ok(sha256(&[D_SIG, &canonical_bytes(record, false)?]))
}

/// The record's hash: what the next record's `prev` holds and what the Merkle tree is built over.
/// It covers the signature, so it names one signed record and nothing else.
pub fn hash(record: &Record) -> Result<Hash, Problem> {
    Ok(sha256(&[D_HASH, &canonical_bytes(record, true)?]))
}

/// `unsigned` with its signature set (whatever `sig` held is replaced).
pub fn sign(unsigned: Record, signer: &dyn Signer) -> Result<Record, Problem> {
    if unsigned.device != signer.device() {
        return Err(Kind::WrongDevice.into());
    }
    let mut record = unsigned;
    record.sig = to_hex(&signer.sign(&signing_digest(&record)?));
    record.check()?;
    Ok(record)
}

fn verify_checked(record: &Record, device: Option<&str>) -> Result<(), Problem> {
    if device.is_some_and(|d| d != record.device) {
        return Err(Kind::WrongDevice.into());
    }
    if !verify_signature(&record.device, &signing_digest(record)?, &record.sig) {
        return Err(Kind::BadSignature.into());
    }
    if let (Some(summary), Some(salt)) = (&record.summary, &record.summary_salt) {
        if summary_hash(salt, summary)? != record.summary_hash {
            return Err(Kind::SummaryMismatch.into());
        }
    }
    Ok(())
}

/// One record on its own: shape, device (when given), signature, and the summary commitment.
/// `Malformed`, `WrongDevice`, `BadSignature` or `SummaryMismatch`.
pub fn verify_record(record: &Record, device: Option<&str>) -> Result<(), Problem> {
    record.check()?;
    verify_checked(record, device)
}

/// [`verify_record`] on a JSON object.
pub fn verify_record_value(record: &Value, device: Option<&str>) -> Result<(), Problem> {
    verify_checked(&Record::from_value(record)?, device)
}

/// The record without its summary. Still verifiable: the signature covers `summary_hash`.
pub fn redact(record: &Record) -> Record {
    Record { summary: None, summary_salt: None, ..record.clone() }
}

// ---------------------------------------------------------------- chain

/// Checks a chain one record at a time, from seq 0.
#[derive(Debug, Clone, Default)]
pub struct ChainVerifier {
    device: Option<String>,
    hashes: Vec<Hash>,
    prev_time: u64,
}

impl ChainVerifier {
    /// `device`: the device the chain must belong to; None takes it from the first record.
    pub fn new(device: Option<&str>) -> ChainVerifier {
        ChainVerifier { device: device.map(str::to_string), hashes: Vec::new(), prev_time: 0 }
    }

    /// How many records have been accepted.
    pub fn size(&self) -> u64 {
        self.hashes.len() as u64
    }

    /// The hashes of the accepted records, in order.
    pub fn hashes(&self) -> &[Hash] {
        &self.hashes
    }

    pub fn device(&self) -> Option<&str> {
        self.device.as_deref()
    }

    /// Accept the next record, or say what is wrong with it (with its index).
    ///
    /// In this order: shape (`Malformed`); device (`WrongDevice`); signature
    /// (`BadSignature`); summary commitment (`SummaryMismatch`); sequence
    /// (`Missing` when a number is skipped, `Duplicate` or `Fork` when one comes
    /// again); link to the record before (`WrongPrev`); time (`TimeWentBack`).
    pub fn push(&mut self, record: &Record) -> Result<(), Problem> {
        let index = self.size();
        self.push_inner(record).map_err(|p| p.at(index))
    }

    /// [`ChainVerifier::push`] for a JSON object.
    pub fn push_value(&mut self, record: &Value) -> Result<(), Problem> {
        let index = self.size();
        let record = Record::from_value(record).map_err(|p| p.at(index))?;
        self.push(&record)
    }

    fn push_inner(&mut self, record: &Record) -> Result<(), Problem> {
        let index = self.size();
        record.check()?;
        let device = self.device.get_or_insert_with(|| record.device.clone()).clone();
        verify_checked(record, Some(&device))?;
        let this = hash(record)?;
        if record.seq > index {
            return Err(Kind::Missing.into());
        }
        if record.seq < index {
            let same = self.hashes[record.seq as usize] == this;
            return Err(if same { Kind::Duplicate } else { Kind::Fork }.into());
        }
        let expected = match self.hashes.last() {
            Some(last) => to_hex(last),
            None => genesis(&device)?,
        };
        if record.prev != expected {
            return Err(Kind::WrongPrev.into());
        }
        if index > 0 && record.time.saturating_add(TIME_TOLERANCE_MS) < self.prev_time {
            return Err(Kind::TimeWentBack.into());
        }
        self.hashes.push(this);
        self.prev_time = record.time;
        Ok(())
    }
}

/// The number of records when the whole chain checks out, or the first problem with its index and kind.
/// See [`ChainVerifier::push`] for the order of checks.
pub fn verify_chain(records: &[Record], device: Option<&str>) -> Result<u64, Problem> {
    let mut verifier = ChainVerifier::new(device);
    for record in records {
        verifier.push(record)?;
    }
    Ok(verifier.size())
}

/// [`verify_chain`] on JSON objects; a record that does not parse is `Malformed` at its index.
pub fn verify_chain_values(records: &[Value], device: Option<&str>) -> Result<u64, Problem> {
    let mut verifier = ChainVerifier::new(device);
    for record in records {
        verifier.push_value(record)?;
    }
    Ok(verifier.size())
}

/// The signed record that follows `head` (None for a device's first record). Pure.
pub fn append(head: Option<&Record>, entry: &Entry, signer: &dyn Signer, ref_key: &dyn RefKey) -> Result<Record, Problem> {
    entry.check()?;
    let summary = check_summary(&entry.summary)?;
    let device = signer.device();
    let (seq, prev) = match head {
        None => (0, genesis(&device)?),
        Some(head) => {
            head.check()?;
            if head.device != device {
                return Err(Kind::WrongDevice.into());
            }
            if head.seq >= MAX_INT {
                return malformed(); // the chain is full
            }
            if entry.time.saturating_add(TIME_TOLERANCE_MS) < head.time {
                return Err(Kind::TimeWentBack.into());
            }
            (head.seq + 1, to_hex(&hash(head)?))
        }
    };
    let salt = summary_salt(ref_key, &device, seq)?;
    let resource = match &entry.resource {
        None => None,
        Some(resource) => Some(resource_ref(ref_key, resource)?),
    };
    let record = Record {
        device,
        seq,
        time: entry.time,
        mandate: entry.mandate.clone(),
        actor: entry.actor,
        action: entry.action.clone(),
        resource,
        summary_hash: summary_hash(&salt, &summary)?,
        outcome: entry.outcome,
        cost: entry.cost.clone(),
        links: entry.links.clone(),
        prev,
        sig: String::new(),
        summary: Some(summary),
        summary_salt: Some(salt),
    };
    sign(record, signer)
}

/// [`append`] from JSON: the entry is read first, then the head (the order the Python twin checks in).
pub fn append_value(head: Option<&Value>, entry: &Value, signer: &dyn Signer, ref_key: &dyn RefKey) -> Result<Record, Problem> {
    let entry = Entry::from_value(entry)?;
    check_summary(&entry.summary)?;
    let head = head.map(Record::from_value).transpose()?;
    append(head.as_ref(), &entry, signer, ref_key)
}

// ---------------------------------------------------------------- checkpoint

/// "Sealed": the device's signature over the Merkle root of its first `size` records.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkpoint {
    pub device: String,
    pub size: u64,
    /// Merkle root, 64 hex.
    pub root: String,
    /// When it was made, milliseconds since 1970-01-01T00:00:00Z.
    pub time: u64,
    pub sig: String,
}

impl Checkpoint {
    pub fn from_value(v: &Value) -> Result<Checkpoint, Problem> {
        let Some(map) = v.as_object() else {
            return malformed();
        };
        if !keys_ok(map, &["device", "size", "root", "time", "sig"], &[]) {
            return malformed();
        }
        let cp = Checkpoint {
            device: text(&map["device"])?,
            size: int(&map["size"])?,
            root: text(&map["root"])?,
            time: int(&map["time"])?,
            sig: text(&map["sig"])?,
        };
        cp.check()?;
        Ok(cp)
    }

    pub fn check(&self) -> Result<(), Problem> {
        if is_hex(&self.device, 64) && is_hex(&self.root, 64) && is_hex(&self.sig, 128) && self.size <= MAX_INT && self.time <= MAX_INT {
            Ok(())
        } else {
            malformed()
        }
    }

    pub fn to_value(&self) -> Value {
        json!({"device": self.device, "size": self.size, "root": self.root, "time": self.time, "sig": self.sig})
    }

    fn digest(&self) -> Result<Hash, Problem> {
        let body = json!({"device": self.device, "size": self.size, "root": self.root, "time": self.time});
        Ok(sha256(&[D_CHECKPOINT, &canonicalize(&body)?]))
    }
}

/// The signed checkpoint over the records with these hashes (all of them, in order) at `time` (ms).
pub fn checkpoint(hashes: &[Hash], time: u64, signer: &dyn Signer) -> Result<Checkpoint, Problem> {
    let mut cp = Checkpoint {
        device: signer.device(),
        size: hashes.len() as u64,
        root: to_hex(&merkle_root(hashes)),
        time,
        sig: String::new(),
    };
    if time > MAX_INT {
        return malformed();
    }
    cp.sig = to_hex(&signer.sign(&cp.digest()?));
    Ok(cp)
}

/// Shape, device (when given) and signature: `Malformed`, `WrongDevice` or `BadSignature`.
pub fn verify_checkpoint(cp: &Checkpoint, device: Option<&str>) -> Result<(), Problem> {
    cp.check()?;
    if device.is_some_and(|d| d != cp.device) {
        return Err(Kind::WrongDevice.into());
    }
    if !verify_signature(&cp.device, &cp.digest()?, &cp.sig) {
        return Err(Kind::BadSignature.into());
    }
    Ok(())
}

// ---------------------------------------------------------------- export

/// A self-describing export: the device, a checkpoint, and either every record
/// up to the checkpoint (`proofs` None) or a subset with one inclusion proof each.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Export {
    pub device: String,
    pub checkpoint: Checkpoint,
    pub records: Vec<Record>,
    /// None: the export is complete. Some: one proof (hashes as 64 hex) per record.
    pub proofs: Option<Vec<Vec<String>>>,
}

/// What a verified export holds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportSummary {
    pub device: String,
    /// How many records the checkpoint covers.
    pub size: u64,
    /// How many records are in the file.
    pub count: u64,
    pub complete: bool,
    /// How many of them have no summary.
    pub redacted: u64,
}

impl Export {
    pub fn complete(&self) -> bool {
        self.proofs.is_none()
    }

    /// An export from its JSON object, strictly; anything off is `Malformed` (no index).
    pub fn from_value(v: &Value) -> Result<Export, Problem> {
        let Some(map) = v.as_object() else {
            return malformed();
        };
        if !keys_ok(map, &["type", "format", "spec", "device", "checkpoint", "complete", "records"], &["proofs"])
            || map["type"].as_str() != Some(EXPORT_TYPE)
            || map["format"].as_u64() != Some(EXPORT_FORMAT)
            || map["spec"].as_str() != Some(SPEC)
        {
            return malformed();
        }
        let (Some(complete), Some(records)) = (map["complete"].as_bool(), map["records"].as_array()) else {
            return malformed();
        };
        let proofs = match map.get("proofs") {
            None => None,
            Some(Value::Array(proofs)) => Some(
                proofs
                    .iter()
                    .map(|proof| proof.as_array().ok_or(Problem::from(Kind::Malformed))?.iter().map(text).collect())
                    .collect::<Result<Vec<Vec<String>>, Problem>>()?,
            ),
            Some(_) => return malformed(),
        };
        if complete != proofs.is_none() {
            return malformed();
        }
        let export = Export {
            device: text(&map["device"])?,
            checkpoint: Checkpoint::from_value(&map["checkpoint"])?,
            records: records.iter().map(Record::from_value).collect::<Result<_, _>>()?,
            proofs,
        };
        export.check()?;
        Ok(export)
    }

    /// The shape of the whole file.
    pub fn check(&self) -> Result<(), Problem> {
        if !is_hex(&self.device, 64) {
            return malformed();
        }
        self.checkpoint.check()?;
        for record in &self.records {
            record.check()?;
        }
        if let Some(proofs) = &self.proofs {
            let ok = proofs.len() == self.records.len()
                && proofs.iter().all(|p| p.len() <= merkle::MAX_INCLUSION_PROOF && p.iter().all(|h| is_hex(h, 64)));
            if !ok {
                return malformed();
            }
        }
        Ok(())
    }

    pub fn to_value(&self) -> Value {
        let mut value = json!({
            "type": EXPORT_TYPE, "format": EXPORT_FORMAT, "spec": SPEC, "device": self.device,
            "checkpoint": self.checkpoint.to_value(), "complete": self.complete(),
            "records": self.records.iter().map(Record::to_value).collect::<Vec<_>>(),
        });
        if let (Some(map), Some(proofs)) = (value.as_object_mut(), &self.proofs) {
            map.insert("proofs".into(), json!(proofs));
        }
        value
    }
}

/// An export of the chain `records` (from seq 0) sealed by checkpoint `cp`.
///
/// `select` None: every record up to the checkpoint. Otherwise the records with
/// those sequence numbers (ascending, each below `cp.size`), each with its
/// inclusion proof. `redact_summaries` leaves the summaries out.
pub fn export(records: &[Record], cp: &Checkpoint, select: Option<&[u64]>, redact_summaries: bool) -> Result<Export, Problem> {
    cp.check()?;
    for record in records {
        record.check()?;
    }
    if (records.len() as u64) < cp.size {
        return Err(Kind::SizeMismatch.into());
    }
    let size = cp.size as usize;
    let hashes = records[..size].iter().map(hash).collect::<Result<Vec<Hash>, Problem>>()?;
    if to_hex(&merkle_root(&hashes)) != cp.root {
        return Err(Kind::RootMismatch.into());
    }
    let keep = |seq: usize| if redact_summaries { redact(&records[seq]) } else { records[seq].clone() };
    let (chosen, proofs) = match select {
        None => ((0..size).map(keep).collect(), None),
        Some(select) => {
            if select.iter().any(|s| *s >= cp.size) || select.windows(2).any(|w| w[1] <= w[0]) {
                return malformed();
            }
            let proofs = select
                .iter()
                .map(|s| Ok(inclusion_proof(&hashes, *s)?.iter().map(|h| to_hex(h)).collect()))
                .collect::<Result<Vec<Vec<String>>, Problem>>()?;
            (select.iter().map(|s| keep(*s as usize)).collect(), Some(proofs))
        }
    };
    Ok(Export { device: cp.device.clone(), checkpoint: cp.clone(), records: chosen, proofs })
}

/// Check an export with nothing but the export.
///
/// In order: the whole file's shape (`Malformed`); the device, when the caller
/// knows which one to expect (`WrongDevice`); the checkpoint (`BadCheckpoint`);
/// then the records. Complete: the chain ([`verify_chain`]'s kinds, with the
/// index), `SizeMismatch`, `RootMismatch`. Subset, per record: `WrongDevice`,
/// `BadSignature`, `SummaryMismatch`; `Malformed` when the sequence numbers do
/// not ascend; `BadProof`; `WrongPrev` between neighbours.
///
/// The device is whatever key the file names. That the key is the person's is
/// for the reader to establish, by comparing it with the key the person's device shows.
pub fn verify_export(export: &Export, device: Option<&str>) -> Result<ExportSummary, Problem> {
    export.check()?;
    if device.is_some_and(|d| d != export.device) {
        return Err(Kind::WrongDevice.into());
    }
    let device = export.device.as_str();
    let cp = &export.checkpoint;
    if verify_checkpoint(cp, Some(device)).is_err() {
        return Err(Kind::BadCheckpoint.into());
    }
    match &export.proofs {
        None => {
            let mut verifier = ChainVerifier::new(Some(device));
            for record in &export.records {
                verifier.push(record)?;
            }
            if verifier.size() != cp.size {
                return Err(Kind::SizeMismatch.into());
            }
            if to_hex(&merkle_root(verifier.hashes())) != cp.root {
                return Err(Kind::RootMismatch.into());
            }
        }
        Some(proofs) => {
            let root = hash_from_hex(&cp.root).ok_or(Problem::from(Kind::Malformed))?;
            let mut previous: Option<(u64, Hash)> = None;
            for (index, (record, proof)) in export.records.iter().zip(proofs).enumerate() {
                let step = || -> Result<Hash, Problem> {
                    verify_checked(record, Some(device))?;
                    if previous.is_some_and(|(seq, _)| record.seq <= seq) {
                        return malformed();
                    }
                    let this = hash(record)?;
                    let path: Vec<Hash> = proof.iter().filter_map(|h| hash_from_hex(h)).collect();
                    if path.len() != proof.len() || !verify_inclusion(&this, record.seq, cp.size, &path, &root) {
                        return Err(Kind::BadProof.into());
                    }
                    if previous.is_some_and(|(seq, prev)| record.seq == seq + 1 && record.prev != to_hex(&prev)) {
                        return Err(Kind::WrongPrev.into());
                    }
                    Ok(this)
                };
                let this = step().map_err(|p| p.at(index as u64))?;
                previous = Some((record.seq, this));
            }
        }
    }
    Ok(ExportSummary {
        device: export.device.clone(),
        size: cp.size,
        count: export.records.len() as u64,
        complete: export.complete(),
        redacted: export.records.iter().filter(|r| r.summary.is_none()).count() as u64,
    })
}

/// [`verify_export`] on a JSON object.
pub fn verify_export_value(export: &Value, device: Option<&str>) -> Result<ExportSummary, Problem> {
    verify_export(&Export::from_value(export)?, device)
}

/// The export as the text of a file: canonical JSON and a final newline.
pub fn export_text(export: &Export) -> Result<String, Problem> {
    let mut text = String::from_utf8(canonicalize(&export.to_value())?).map_err(|_| Problem::from(Kind::Malformed))?;
    text.push('\n');
    Ok(text)
}

/// [`verify_export`] on the text of a file, read strictly (see [`jcs::loads_strict`]).
pub fn verify_export_text(text: &str, device: Option<&str>) -> Result<ExportSummary, Problem> {
    verify_export_value(&loads_strict(text)?, device)
}
