//! Where ciphertext is kept. The core opens no files: the app shell provides a
//! [`VaultStore`] (SQLite with SQLCipher is planned there); [`MemoryStore`] is
//! for tests.
//!
//! A store sees only: the header (public by design), record ids (random),
//! kind tags and index tags (keyed hashes), and two opaque blobs per record.
//! Nothing the store returns is trusted: the vault authenticates all of it.

use std::collections::BTreeMap;
use std::fmt;

/// A store failed. Carries no detail on purpose.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StoreError;

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the vault store could not complete the operation")
    }
}

impl std::error::Error for StoreError {}

/// One record as stored. `head` holds the wrapped record key (small, rewritten
/// on rotation); `body` holds the encrypted content (never rewritten by rotation).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredRecord {
    /// Keyed hash of the kind; empty for a tombstone.
    pub kind_tag: String,
    /// Blind index tags of the record's indexed fields.
    pub index_tags: Vec<String>,
    pub head: Vec<u8>,
    /// Empty for a tombstone.
    pub body: Vec<u8>,
}

/// One step of an atomic batch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreOp {
    /// Replace the header.
    PutHeader(Vec<u8>),
    /// Insert or replace a whole record.
    PutRecord { id: String, record: StoredRecord },
    /// Replace the head and tags of an existing record, leaving its body as it is.
    PutHead { id: String, kind_tag: String, index_tags: Vec<String>, head: Vec<u8> },
    /// Remove a record physically.
    Delete { id: String },
}

/// Storage for one vault.
pub trait VaultStore {
    /// The header, if a vault has been created here.
    fn header(&self) -> Result<Option<Vec<u8>>, StoreError>;
    fn get(&self, id: &str) -> Result<Option<StoredRecord>, StoreError>;
    /// Only the head of a record (rotation reads every head and no body).
    fn head(&self, id: &str) -> Result<Option<Vec<u8>>, StoreError>;
    /// Ids filed under a kind tag, in id order.
    fn list(&self, kind_tag: &str) -> Result<Vec<String>, StoreError>;
    /// Ids filed under a kind tag that carry an index tag, in id order.
    fn find(&self, kind_tag: &str, index_tag: &str) -> Result<Vec<String>, StoreError>;
    /// Every record id, tombstones included, in id order.
    fn ids(&self) -> Result<Vec<String>, StoreError>;
    /// Applies every step or none.
    fn apply(&mut self, batch: Vec<StoreOp>) -> Result<(), StoreError>;

    fn put(&mut self, id: &str, record: StoredRecord) -> Result<(), StoreError> {
        self.apply(vec![StoreOp::PutRecord { id: id.to_string(), record }])
    }

    fn delete(&mut self, id: &str) -> Result<(), StoreError> {
        self.apply(vec![StoreOp::Delete { id: id.to_string() }])
    }
}

/// A store in memory, for tests. `Clone` gives a snapshot (a "backup of the disk").
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MemoryStore {
    pub header: Option<Vec<u8>>,
    pub records: BTreeMap<String, StoredRecord>,
    /// Test hook: the next `apply` fails and changes nothing.
    pub fail_next_apply: bool,
}

impl MemoryStore {
    pub fn new() -> Self {
        MemoryStore::default()
    }
}

impl VaultStore for MemoryStore {
    fn header(&self) -> Result<Option<Vec<u8>>, StoreError> {
        Ok(self.header.clone())
    }

    fn get(&self, id: &str) -> Result<Option<StoredRecord>, StoreError> {
        Ok(self.records.get(id).cloned())
    }

    fn head(&self, id: &str) -> Result<Option<Vec<u8>>, StoreError> {
        Ok(self.records.get(id).map(|r| r.head.clone()))
    }

    fn list(&self, kind_tag: &str) -> Result<Vec<String>, StoreError> {
        Ok(self.records.iter().filter(|(_, r)| r.kind_tag == kind_tag).map(|(id, _)| id.clone()).collect())
    }

    fn find(&self, kind_tag: &str, index_tag: &str) -> Result<Vec<String>, StoreError> {
        Ok(self
            .records
            .iter()
            .filter(|(_, r)| r.kind_tag == kind_tag && r.index_tags.iter().any(|t| t == index_tag))
            .map(|(id, _)| id.clone())
            .collect())
    }

    fn ids(&self) -> Result<Vec<String>, StoreError> {
        Ok(self.records.keys().cloned().collect())
    }

    fn apply(&mut self, batch: Vec<StoreOp>) -> Result<(), StoreError> {
        if self.fail_next_apply {
            self.fail_next_apply = false;
            return Err(StoreError);
        }
        // Work on a copy so a failing step leaves nothing half done.
        let mut next = self.clone();
        for op in batch {
            match op {
                StoreOp::PutHeader(bytes) => next.header = Some(bytes),
                StoreOp::PutRecord { id, record } => {
                    next.records.insert(id, record);
                }
                StoreOp::PutHead { id, kind_tag, index_tags, head } => {
                    let record = next.records.get_mut(&id).ok_or(StoreError)?;
                    record.kind_tag = kind_tag;
                    record.index_tags = index_tags;
                    record.head = head;
                }
                StoreOp::Delete { id } => {
                    next.records.remove(&id);
                }
            }
        }
        *self = next;
        Ok(())
    }
}
