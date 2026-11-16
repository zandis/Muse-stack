//! Where a chain is kept. Not part of the format, and no storage lives in this
//! crate: the vault supplies an encrypted [`RecordStore`]; [`MemoryStore`] is for tests.
//!
//! A store keeps records in order and must refuse an append that does not
//! extend the head ([`check_extends`]): a second record for a taken sequence
//! number would be a fork signed by this very device. A store shared between
//! processes has to make "read the head, sign, append" one atomic step (the
//! Python home does it inside one SQLite write transaction).

use super::{append, genesis, hash, to_hex, Entry, Kind, Problem, Record, RefKey, Signer};

/// The four operations the records code needs from storage.
pub trait RecordStore {
    /// The store's own failure (I/O, decryption); a [`Problem`] must fit in it.
    type Error: From<Problem>;

    /// The record with this sequence number, if the store has it.
    fn get(&self, seq: u64) -> Result<Option<Record>, Self::Error>;

    /// How many records the store holds (the next sequence number).
    fn len(&self) -> Result<u64, Self::Error>;

    /// The newest record.
    fn head(&self) -> Result<Option<Record>, Self::Error>;

    /// Add `record` if it is the next one after the head; a [`Problem`]
    /// (`Missing`, `Fork`, `WrongDevice`, `WrongPrev`) otherwise.
    fn append(&mut self, record: Record) -> Result<(), Self::Error>;

    fn is_empty(&self) -> Result<bool, Self::Error> {
        Ok(self.len()? == 0)
    }
}

/// Is `record` the one that may follow a chain of `size` records ending in `head`?
pub fn check_extends(head: Option<&Record>, size: u64, record: &Record) -> Result<(), Problem> {
    record.check()?;
    if record.seq > size {
        return Err(Kind::Missing.into());
    }
    if record.seq < size {
        return Err(Kind::Fork.into());
    }
    let expected = match head {
        None => genesis(&record.device)?,
        Some(head) => {
            if head.device != record.device {
                return Err(Kind::WrongDevice.into());
            }
            to_hex(&hash(head)?)
        }
    };
    if record.prev != expected {
        return Err(Kind::WrongPrev.into());
    }
    Ok(())
}

/// Sign the record that follows the store's head and append it.
///
/// Only safe when nothing else appends between the two steps; a shared store
/// must offer its own atomic version of this.
pub fn write<S: RecordStore>(store: &mut S, entry: &Entry, signer: &dyn Signer, ref_key: &dyn RefKey) -> Result<Record, S::Error> {
    let head = store.head()?;
    let record = append(head.as_ref(), entry, signer, ref_key)?;
    store.append(record.clone())?;
    Ok(record)
}

/// A [`RecordStore`] in a vector, for tests.
#[derive(Debug, Clone, Default)]
pub struct MemoryStore {
    records: Vec<Record>,
}

impl MemoryStore {
    pub fn new() -> MemoryStore {
        MemoryStore::default()
    }

    /// Every record, in order.
    pub fn records(&self) -> &[Record] {
        &self.records
    }
}

impl RecordStore for MemoryStore {
    type Error = Problem;

    fn get(&self, seq: u64) -> Result<Option<Record>, Problem> {
        Ok(usize::try_from(seq).ok().and_then(|i| self.records.get(i)).cloned())
    }

    fn len(&self) -> Result<u64, Problem> {
        Ok(self.records.len() as u64)
    }

    fn head(&self) -> Result<Option<Record>, Problem> {
        Ok(self.records.last().cloned())
    }

    fn append(&mut self, record: Record) -> Result<(), Problem> {
        check_extends(self.records.last(), self.records.len() as u64, &record)?;
        self.records.push(record);
        Ok(())
    }
}
