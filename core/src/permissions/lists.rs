//! The revocation list and the spent set (interface document, sections 7 and 8).
//! Neither touches storage: both are values the caller loads and saves.

use std::collections::{BTreeMap, HashMap};

use biscuit_auth::builder::Term;
use biscuit_auth::{BiscuitBuilder, KeyPair, UnverifiedBiscuit};
use serde_json::{json, Value};

use super::model::*;
use super::token::{parse_public_key, public_key_text, rfc3339};

/// One signed statement "this identifier is revoked": a one-block Biscuit under the root key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RevocationEntry {
    pub id: String,
    pub at: i64,
    pub signer: String,
    pub token: String,
}

fn entry_text(id: &str, at: i64, signer: &str) -> String {
    format!("schema(\"muse.revocation\", 1);\nrevoked(\"{id}\", {});\nsigner(\"{signer}\");\n", rfc3339(at))
}

impl RevocationEntry {
    /// Sign a new entry with the root key.
    pub fn sign(id: &str, at: i64, root: &KeyPair) -> Result<RevocationEntry, PermissionError> {
        if !is_revocation_id(id) || !is_time(at) {
            return fail("invalid_revocation", "a revocation needs a 128-hex identifier and a time");
        }
        let signer = public_key_text(root);
        let source = format!("schema(\"muse.revocation\", 1);\nrevoked({{id}}, {});\nsigner({{signer}});\n", rfc3339(at));
        let params = HashMap::from([("id".to_string(), Term::Str(id.to_string())), ("signer".to_string(), Term::Str(signer.clone()))]);
        let lib = |e: biscuit_auth::error::Token| PermissionError::new("library_error", e.to_string());
        let biscuit = BiscuitBuilder::new().code_with_params(&source, params, HashMap::new()).and_then(|b| b.build(root)).map_err(lib)?;
        let token = biscuit.to_base64().map_err(lib)?;
        RevocationEntry::verify(&token, &signer)
    }

    /// Verify an entry under the root key and read it.
    pub fn verify(token: &str, root: &str) -> Result<RevocationEntry, PermissionError> {
        let bad = |why: &str| PermissionError::new("invalid_revocation", why.to_string());
        let key = parse_public_key(root)?;
        let biscuit = UnverifiedBiscuit::from_base64(token).map_err(|_| bad("not a token"))?.verify(key).map_err(|_| bad("not signed by the root key"))?;
        if biscuit.block_count() != 1 {
            return Err(bad("an entry has exactly one block"));
        }
        let text = biscuit.print_block_source(0).map_err(|_| bad("unreadable"))?;
        let parsed = (|| {
            let rest = text.strip_prefix("schema(\"muse.revocation\", 1);\nrevoked(\"")?;
            let (id, rest) = rest.split_once("\", ")?;
            let (date, _) = rest.split_once(");\n")?;
            Some((id.to_string(), date.to_string()))
        })();
        let (id, date) = parsed.ok_or_else(|| bad("not a muse.revocation entry"))?;
        // The time is read by finding the one second that prints as this date.
        let at = parse_date(&date).ok_or_else(|| bad("bad time"))?;
        if !is_revocation_id(&id) || text != entry_text(&id, at, root) {
            return Err(bad("not in the schema's form, or names another signer"));
        }
        Ok(RevocationEntry { id, at, signer: root.to_string(), token: token.to_string() })
    }
}

fn parse_date(text: &str) -> Option<i64> {
    let b = text.as_bytes();
    if b.len() != 20 || !text.is_ascii() {
        return None;
    }
    let num = |from: usize, to: usize| text[from..to].parse::<i64>().ok();
    let days = crate::civil::days_from_civil(num(0, 4)?, num(5, 7)?, num(8, 10)?);
    let t = days * 86_400 + num(11, 13)? * 3600 + num(14, 16)? * 60 + num(17, 19)?;
    (is_time(t) && rfc3339(t) == text).then_some(t)
}

/// A set of revocation entries. It only grows; merging two lists is set union.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RevocationList {
    entries: BTreeMap<String, RevocationEntry>,
}

impl RevocationList {
    pub fn new() -> RevocationList {
        RevocationList::default()
    }

    /// Revoke `id` (a block's revocation identifier) at time `at`, signed by the root key.
    pub fn add(&mut self, id: &str, at: i64, root: &KeyPair) -> Result<(), PermissionError> {
        self.insert(RevocationEntry::sign(id, at, root)?);
        Ok(())
    }

    /// Add an entry received from elsewhere, after verifying it under the root key.
    pub fn add_entry(&mut self, token: &str, root: &str) -> Result<(), PermissionError> {
        self.insert(RevocationEntry::verify(token, root)?);
        Ok(())
    }

    /// For one identifier keep the earlier time, then the smaller text: any order of merges converges.
    fn insert(&mut self, entry: RevocationEntry) {
        match self.entries.get(&entry.id) {
            Some(old) if (old.at, &old.token) <= (entry.at, &entry.token) => {}
            _ => {
                self.entries.insert(entry.id.clone(), entry);
            }
        }
    }

    /// Set union with another list (whose entries were verified when it was built).
    pub fn merge(&mut self, other: &RevocationList) {
        for entry in other.entries.values() {
            self.insert(entry.clone());
        }
    }

    /// Verify every entry again under `root`.
    pub fn verify(&self, root: &str) -> Result<(), PermissionError> {
        self.entries.values().try_for_each(|e| RevocationEntry::verify(&e.token, root).map(|_| ()))
    }

    pub fn contains(&self, id: &str) -> bool {
        self.entries.contains_key(id)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Entries sorted by identifier.
    pub fn entries(&self) -> Vec<&RevocationEntry> {
        self.entries.values().collect()
    }

    /// `{"v": 1, "entries": [...]}`, entries sorted by identifier.
    pub fn to_json(&self) -> String {
        let tokens: Vec<&str> = self.entries.values().map(|e| e.token.as_str()).collect();
        json!({"v": 1, "entries": tokens}).to_string()
    }

    /// Load a list, verifying every entry. One bad entry refuses the whole file.
    pub fn from_json(text: &str, root: &str) -> Result<RevocationList, PermissionError> {
        let bad = || PermissionError::new("invalid_revocation", "not a revocation list");
        let value: Value = serde_json::from_str(text).map_err(|_| bad())?;
        if value.get("v").and_then(Value::as_i64) != Some(1) {
            return Err(bad());
        }
        let mut list = RevocationList::new();
        for token in value.get("entries").and_then(Value::as_array).ok_or_else(bad)? {
            list.add_entry(token.as_str().ok_or_else(bad)?, root)?;
        }
        Ok(list)
    }
}

/// Single-use ids already used, each kept until its token has expired. Bounded: when it
/// is full of live entries, `insert` fails and the caller must refuse the request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpentSet {
    capacity: usize,
    entries: BTreeMap<String, i64>,
}

impl Default for SpentSet {
    fn default() -> Self {
        SpentSet::new(10_000)
    }
}

impl SpentSet {
    pub fn new(capacity: usize) -> SpentSet {
        SpentSet { capacity, entries: BTreeMap::new() }
    }

    /// True if `key` was used and its token could still be valid at `now`.
    pub fn contains(&self, key: &str, now: i64) -> bool {
        self.entries.get(key).is_some_and(|until| *until >= now)
    }

    /// Drop entries whose time has passed.
    pub fn purge(&mut self, now: i64) {
        self.entries.retain(|_, until| *until >= now);
    }

    /// Whether `extra` more entries fit once expired ones are dropped.
    pub fn has_room(&self, extra: usize, now: i64) -> bool {
        self.entries.values().filter(|until| **until >= now).count() + extra <= self.capacity
    }

    /// Remember `key` until `until`.
    pub fn insert(&mut self, key: &str, until: i64, now: i64) -> Result<(), PermissionError> {
        self.purge(now);
        if !self.entries.contains_key(key) && self.entries.len() >= self.capacity {
            return fail("spent_set_full", "no room to remember another single-use id");
        }
        let kept = self.entries.get(key).copied().unwrap_or(until).max(until);
        self.entries.insert(key.to_string(), kept);
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Rebuild a set from stored (key, until) pairs.
    pub fn from_entries(entries: &[(String, i64)], capacity: usize) -> SpentSet {
        SpentSet { capacity, entries: entries.iter().cloned().collect() }
    }

    /// (key, until) pairs sorted by key, for the caller to store.
    pub fn entries(&self) -> Vec<(String, i64)> {
        self.entries.iter().map(|(k, v)| (k.clone(), *v)).collect()
    }
}
