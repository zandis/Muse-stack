//! The vault: keys and private records that only the person's devices can read.
//!
//! The format (key hierarchy, wrap slots, record layout, export) and the
//! reasons for each choice are in `docs/spec/interfaces/vault-format.md`.
//! In short:
//!
//! - One random master key per person. It is never stored in the clear and no
//!   function returns it. Purpose keys are derived from it with HKDF.
//! - The header lists *wrap slots*: one per device (secret held by the
//!   device's keystore), exactly one recovery key (24 words), and optional
//!   passkeys (PRF). Each slot is an X25519 public key, so a rotation can
//!   re-wrap for devices that are not present.
//! - Each record has its own random key (XChaCha20-Poly1305); the record key
//!   is wrapped under a key derived from the master key. A rotation re-wraps
//!   the small heads and leaves content alone.
//! - The core does no I/O: storage is a [`VaultStore`], the device secret a
//!   [`DeviceKeyStore`], randomness a [`VaultRng`], and `now` an argument.
//!
//! This code has had no outside cryptographic review. It needs one before it
//! holds anything real.

mod export;
pub mod header;
pub mod keystore;
pub mod mnemonic;
pub mod primitives;
pub mod record;
pub mod secret;
pub mod store;

use std::fmt;

use zeroize::{Zeroize, Zeroizing};

pub use header::{Header, Slot};
pub use keystore::{Argon2Params, DeviceKeyStore, KeyStoreError, MemoryKeyStore, PassphraseKeyStore};
pub use mnemonic::{Language, MnemonicError};
pub use secret::{OsRng, Secret32, TestRng, VaultRng};
pub use store::{MemoryStore, StoreError, StoreOp, StoredRecord, VaultStore};

use header::{kdf_for, Previous, HEADER_FORMAT, KIND_DEVICE, KIND_PRF, KIND_RECOVERY, MAX_PREVIOUS, MAX_SLOTS, VERSION};
use primitives::{
    index_tag, keyed_ref, kind_tag, normalise_index_value, open, seal, slot_private, subkey, unwrap_from_slot,
    word_check_table, wrap_to_slot, x25519_public, MAX_PAYLOAD, NONCE_LEN,
};
use record::{
    head_meta, open_body, open_head, seal_body, seal_head, valid_name, HeadMeta, HeadPlain, MAX_FIELDS,
    MAX_FIELD_VALUE, STATE_LIVE, STATE_TOMBSTONE,
};
use secret::{ct_eq, hex, random_id, unhex, unhex_array};

/// Why a vault call failed. No variant carries content, keys or words.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VaultError {
    /// The vault is locked; unlock it first.
    Locked,
    /// The vault did not open. One answer for a wrong key, a wrong slot and an
    /// altered header, so a failure says nothing about which part was wrong.
    OpenFailed,
    /// The header (or another structure named here) is not in the expected shape.
    Malformed(&'static str),
    /// The header is from a format version this code does not read.
    UnsupportedVersion,
    /// No such record or slot.
    NotFound,
    /// The record was deleted.
    Deleted,
    /// Stored data failed authentication: altered, swapped, or from another vault.
    Tampered,
    /// An argument is not acceptable (which one is named).
    Invalid(&'static str),
    /// The content is larger than a record may be.
    TooLarge,
    /// The store already holds a vault.
    Exists,
    /// The header is older than one this device has already seen.
    Rollback,
    Store,
    KeyStore,
    /// The random source failed.
    Rng,
    Mnemonic(MnemonicError),
    ExportFailed,
    /// The archive did not open or did not verify. Nothing was written.
    ImportFailed,
}

impl fmt::Display for VaultError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            VaultError::Locked => f.write_str("the vault is locked"),
            VaultError::OpenFailed => f.write_str("the vault could not be opened"),
            VaultError::Malformed(what) => write!(f, "malformed {what}"),
            VaultError::UnsupportedVersion => f.write_str("unsupported vault format version"),
            VaultError::NotFound => f.write_str("not found"),
            VaultError::Deleted => f.write_str("the record was deleted"),
            VaultError::Tampered => f.write_str("stored data failed authentication"),
            VaultError::Invalid(what) => write!(f, "invalid {what}"),
            VaultError::TooLarge => f.write_str("the content is too large for a record"),
            VaultError::Exists => f.write_str("the store already holds a vault"),
            VaultError::Rollback => f.write_str("the vault header is older than expected"),
            VaultError::Store => f.write_str("the vault store failed"),
            VaultError::KeyStore => f.write_str("the device keystore failed"),
            VaultError::Rng => f.write_str("the random source failed"),
            VaultError::Mnemonic(error) => error.fmt(f),
            VaultError::ExportFailed => f.write_str("the export could not be made"),
            VaultError::ImportFailed => f.write_str("the archive could not be opened or verified"),
        }
    }
}

impl std::error::Error for VaultError {}

impl From<StoreError> for VaultError {
    fn from(_: StoreError) -> Self {
        VaultError::Store
    }
}

impl From<KeyStoreError> for VaultError {
    fn from(_: KeyStoreError) -> Self {
        VaultError::KeyStore
    }
}

impl From<MnemonicError> for VaultError {
    fn from(error: MnemonicError) -> Self {
        VaultError::Mnemonic(error)
    }
}

// ---------------------------------------------------------------- recovery key

/// The recovery key: 256 random bits, shown to the person as 24 words.
///
/// It exists in memory only between being made (or typed in) and being
/// dropped; the vault keeps no copy from which the words could be shown again.
pub struct RecoveryKey {
    entropy: Secret32,
}

impl fmt::Debug for RecoveryKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("RecoveryKey(<redacted>)")
    }
}

impl RecoveryKey {
    pub fn generate(rng: &mut dyn VaultRng) -> Result<Self, VaultError> {
        Ok(RecoveryKey { entropy: Secret32::random(rng)? })
    }

    pub fn from_entropy(entropy: Secret32) -> Self {
        RecoveryKey { entropy }
    }

    /// The 24 words in `language`, joined with that language's separator.
    /// The same key has words in every language; any of them opens the vault.
    pub fn words(&self, language: Language) -> Zeroizing<String> {
        mnemonic::entropy_to_words(language, self.entropy.expose()).expect("32 bytes is a valid entropy length")
    }

    /// One word by 1-based position (1..=24).
    pub fn word(&self, language: Language, position: usize) -> Option<&'static str> {
        let indices = self.indices();
        language.word(*indices.get(position.checked_sub(1)?)?)
    }

    /// Reads 24 typed words in one language.
    pub fn from_words(language: Language, text: &str) -> Result<Self, MnemonicError> {
        let entropy = mnemonic::words_to_entropy(language, text)?;
        let entropy = Secret32::from_slice(&entropy).ok_or(MnemonicError::WordCount)?;
        Ok(RecoveryKey { entropy })
    }

    /// Reads 24 typed words, working out the language (no word is in two lists).
    pub fn from_words_any(text: &str) -> Result<(Self, Language), MnemonicError> {
        let mut first = None;
        for language in Language::ALL {
            match RecoveryKey::from_words(language, text) {
                Ok(key) => return Ok((key, language)),
                // A list that knew the first word is the likely language: prefer its error.
                Err(error) if !matches!(error, MnemonicError::UnknownWord(1)) => first = first.or(Some(error)),
                Err(_) => {}
            }
        }
        Err(first.unwrap_or(MnemonicError::UnknownWord(1)))
    }

    /// For "I'll save it later": the key wrapped by this device's keystore, to
    /// be kept on this device only and deleted once the words are confirmed.
    pub fn to_pending(&self, keystore: &dyn DeviceKeyStore) -> Result<Vec<u8>, VaultError> {
        Ok(keystore.wrap(self.entropy.expose())?)
    }

    pub fn from_pending(keystore: &dyn DeviceKeyStore, blob: &[u8]) -> Result<Self, VaultError> {
        let plain = keystore.unwrap(blob)?;
        Ok(RecoveryKey { entropy: Secret32::from_slice(&plain).ok_or(VaultError::KeyStore)? })
    }

    /// The age recipient (`age1...`, public) that exports for this key are encrypted to.
    pub fn export_recipient(&self) -> Result<String, VaultError> {
        export::recipient_for(&self.entropy)
    }

    fn indices(&self) -> Zeroizing<[u16; 24]> {
        let all = mnemonic::entropy_to_indices(self.entropy.expose()).expect("32 bytes is a valid entropy length");
        let mut out = Zeroizing::new([0u16; 24]);
        out.copy_from_slice(&all);
        out
    }
}

// ---------------------------------------------------------------- keys in memory

/// Everything derived from the master key while the vault is unlocked.
/// Dropping it wipes every key.
struct Keys {
    vmk: Secret32,
    record: Secret32,
    index: Secret32,
    reference: Secret32,
    header: Secret32,
    words: Secret32,
    previous: Secret32,
    export_mac: Secret32,
    /// Retired epochs: (epoch, master key, record-wrap key).
    older: Vec<(u64, Secret32, Secret32)>,
}

impl Keys {
    fn derive(vmk: Secret32, vault_id: &[u8; 16]) -> Keys {
        Keys {
            record: subkey(&vmk, vault_id, "record-wrap"),
            index: subkey(&vmk, vault_id, "index"),
            reference: subkey(&vmk, vault_id, "ref"),
            header: subkey(&vmk, vault_id, "header-mac"),
            words: subkey(&vmk, vault_id, "words"),
            previous: subkey(&vmk, vault_id, "previous"),
            export_mac: subkey(&vmk, vault_id, "export-mac"),
            older: Vec::new(),
            vmk,
        }
    }

    fn record_key(&self, epoch: u64, current: u64) -> Option<&Secret32> {
        if epoch == current {
            return Some(&self.record);
        }
        self.older.iter().find(|(e, _, _)| *e == epoch).map(|(_, _, key)| key)
    }
}

fn previous_aad(vault_id: &[u8; 16], old_epoch: u64, current_epoch: u64) -> Vec<u8> {
    let mut aad = b"muse-vault/v1/previous".to_vec();
    aad.extend_from_slice(vault_id);
    aad.extend_from_slice(&old_epoch.to_be_bytes());
    aad.extend_from_slice(&current_epoch.to_be_bytes());
    aad
}

fn word_check_aad(vault_id: &[u8; 16], slot_id: &str, epoch: u64) -> Vec<u8> {
    let mut aad = b"muse-vault/v1/word-check-box".to_vec();
    aad.extend_from_slice(vault_id);
    aad.extend_from_slice(&unhex(slot_id).unwrap_or_default());
    aad.extend_from_slice(&epoch.to_be_bytes());
    aad
}

fn random_nonce(rng: &mut dyn VaultRng) -> Result<[u8; NONCE_LEN], VaultError> {
    let mut nonce = [0u8; NONCE_LEN];
    rng.fill(&mut nonce)?;
    Ok(nonce)
}

/// (Re)wraps the master key to a slot's public key for `epoch`.
fn wrap_slot(slot: &mut Slot, vault_id: &[u8; 16], epoch: u64, vmk: &Secret32, rng: &mut dyn VaultRng) -> Result<(), VaultError> {
    let eph = Secret32::random(rng)?;
    let nonce = random_nonce(rng)?;
    let public: [u8; 32] = unhex_array(&slot.public).ok_or(VaultError::Malformed("slot"))?;
    let (eph_public, wrapped) =
        wrap_to_slot(&eph, &nonce, &public, &slot.aad(vault_id, epoch), vmk).ok_or(VaultError::Invalid("slot public key"))?;
    slot.eph = hex(&eph_public);
    slot.nonce = hex(&nonce);
    slot.wrapped = hex(&wrapped);
    Ok(())
}

fn empty_slot(id: String, kind: &str, label: &str, created: u64, public: &[u8; 32]) -> Slot {
    Slot {
        id,
        kind: kind.to_string(),
        label: label.to_string(),
        created,
        kdf: kdf_for(kind).unwrap_or_default().to_string(),
        public: hex(public),
        eph: String::new(),
        nonce: String::new(),
        wrapped: String::new(),
        keystore_blob: String::new(),
        credential_id: String::new(),
        prf_salt: String::new(),
        issuer: String::new(),
        confirmed: 0,
        export_recipient: String::new(),
        check_salt: String::new(),
        check_nonce: String::new(),
        check_box: String::new(),
    }
}

/// A recovery slot for `recovery`, not yet wrapped.
fn recovery_slot(
    keys: &Keys,
    vault_id: &[u8; 16],
    epoch: u64,
    recovery: &RecoveryKey,
    issuer: &str,
    rng: &mut dyn VaultRng,
    now: u64,
) -> Result<Slot, VaultError> {
    let public = x25519_public(&slot_private(&recovery.entropy, vault_id, KIND_RECOVERY));
    let mut slot = empty_slot(random_id(rng)?, KIND_RECOVERY, "", now, &public);
    slot.issuer = issuer.to_string();
    slot.export_recipient = export::recipient_for(&recovery.entropy)?;
    let mut salt = [0u8; 16];
    rng.fill(&mut salt)?;
    let nonce = random_nonce(rng)?;
    let table = word_check_table(&salt, &recovery.indices());
    slot.check_salt = hex(&salt);
    slot.check_nonce = hex(&nonce);
    slot.check_box = hex(&seal(&keys.words, &nonce, &word_check_aad(vault_id, &slot.id, epoch), table.as_slice()));
    Ok(slot)
}

/// Tries one slot with its 32-byte secret. `None` for every kind of failure.
fn try_slot(header: &Header, slot: &Slot, secret: &Secret32) -> Option<Keys> {
    let vault_id = header.vault_id_bytes();
    let private = slot_private(secret, &vault_id, &slot.kind);
    if !ct_eq(&x25519_public(&private), &unhex(&slot.public)?) {
        return None;
    }
    let eph: [u8; 32] = unhex_array(&slot.eph)?;
    let nonce: [u8; NONCE_LEN] = unhex_array(&slot.nonce)?;
    let vmk = unwrap_from_slot(&private, &eph, &nonce, &slot.aad(&vault_id, header.epoch), &unhex(&slot.wrapped)?)?;
    let mut keys = Keys::derive(vmk, &vault_id);
    if !header.verify_mac(&keys.header) {
        return None;
    }
    for entry in &header.previous {
        let nonce: [u8; NONCE_LEN] = unhex_array(&entry.nonce)?;
        let aad = previous_aad(&vault_id, entry.epoch, header.epoch);
        let old = Secret32::from_slice(&open(&keys.previous, &nonce, &aad, &unhex(&entry.sealed)?)?)?;
        let old_record = subkey(&old, &vault_id, "record-wrap");
        keys.older.push((entry.epoch, old, old_record));
    }
    Some(keys)
}

// ---------------------------------------------------------------- public data types

/// A decrypted record. The content is wiped when this is dropped.
pub struct Record {
    pub id: String,
    pub kind: String,
    pub version: u64,
    pub schema: u32,
    pub payload: Zeroizing<Vec<u8>>,
    /// The indexed fields as stored: (name, normalised value).
    pub fields: Vec<(String, String)>,
}

impl fmt::Debug for Record {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Record")
            .field("id", &self.id)
            .field("kind", &self.kind)
            .field("version", &self.version)
            .field("schema", &self.schema)
            .field("payload", &format_args!("<{} bytes>", self.payload.len()))
            .field("fields", &format_args!("<{} indexed>", self.fields.len()))
            .finish()
    }
}

impl Drop for Record {
    fn drop(&mut self) {
        for (_, value) in self.fields.iter_mut() {
            value.zeroize();
        }
    }
}

/// What a new device sends to an unlocked one to be let in. Nothing in it is
/// secret: the slot secret stays in the new device's keystore.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnrollRequest {
    pub label: String,
    pub public: [u8; 32],
    pub keystore_blob: Vec<u8>,
}

/// One way into the vault, as shown on the Devices screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotInfo {
    pub id: String,
    pub kind: String,
    pub label: String,
    pub created: u64,
}

/// What a passkey prompt needs for one PRF slot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrfSlotInfo {
    pub slot_id: String,
    pub label: String,
    pub credential_id: Vec<u8>,
    /// The PRF input (`eval.first`) for this slot.
    pub prf_salt: [u8; 32],
}

/// What a rotation does with the recovery key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryPolicy {
    /// Keep the same 24 words where that is safe. They are replaced anyway
    /// when the device slot that created them is no longer in the vault
    /// (it is the one being removed, or the vault was restored from a
    /// backup), because that device may have kept them (see
    /// `RecoveryKey::to_pending`).
    Keep,
    /// Make new words. The old ones stop opening the vault (they still open old backups).
    Renew,
}

/// The result of a rotation.
#[derive(Debug)]
pub struct Rotation {
    pub epoch: u64,
    /// Record heads re-wrapped under the new key.
    pub rewrapped: usize,
    /// Record heads that could not be opened and were left as they were.
    pub skipped: usize,
    /// New recovery words, if the rotation made them. Show them now.
    pub new_recovery: Option<RecoveryKey>,
}

// ---------------------------------------------------------------- the vault

/// An open (or locked) vault over a store.
pub struct Vault<S: VaultStore> {
    store: S,
    header: Header,
    keys: Option<Keys>,
    /// The id of the device slot this vault was opened with ("" otherwise).
    device_slot: String,
    auto_lock_secs: Option<u64>,
    last_activity: u64,
}

impl<S: VaultStore> fmt::Debug for Vault<S> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Vault")
            .field("vault_id", &self.header.vault_id)
            .field("epoch", &self.header.epoch)
            .field("slots", &self.header.slots.len())
            .field("locked", &self.keys.is_none())
            .finish()
    }
}

fn record_id_bytes(id: &str) -> Result<[u8; 16], VaultError> {
    unhex_array(id).ok_or(VaultError::Invalid("record id"))
}

fn check_label(label: &str) -> Result<(), VaultError> {
    if label.is_empty() || label.len() > 64 || label.chars().any(char::is_control) {
        return Err(VaultError::Invalid("device label"));
    }
    Ok(())
}

fn normalise_fields(kind: &str, fields: &[(&str, &str)]) -> Result<Vec<(String, String)>, VaultError> {
    if !valid_name(kind) {
        return Err(VaultError::Invalid("kind"));
    }
    if fields.len() > MAX_FIELDS {
        return Err(VaultError::Invalid("fields"));
    }
    let mut out = Vec::with_capacity(fields.len());
    for (name, value) in fields {
        let value = normalise_index_value(value);
        if !valid_name(name) || value.len() > MAX_FIELD_VALUE {
            return Err(VaultError::Invalid("field"));
        }
        out.push((name.to_string(), value));
    }
    Ok(out)
}

fn tags_for(k_index: &Secret32, plain: &HeadPlain) -> (String, Vec<String>) {
    let tags = plain.fields.iter().map(|(name, value)| index_tag(k_index, &plain.kind, name, value)).collect();
    (kind_tag(k_index, &plain.kind), tags)
}

impl<S: VaultStore> Vault<S> {
    // ------------------------------------------------------------ create and open

    /// Makes a new vault in an empty store, with one device slot (for
    /// `keystore`) and a recovery key. Show the returned words to the person,
    /// then call [`Vault::verify_recovery_words`].
    pub fn create(
        mut store: S,
        device_label: &str,
        keystore: &dyn DeviceKeyStore,
        rng: &mut dyn VaultRng,
        now: u64,
    ) -> Result<(Self, RecoveryKey), VaultError> {
        check_label(device_label)?;
        if store.header()?.is_some() || !store.ids()?.is_empty() {
            return Err(VaultError::Exists);
        }
        let vault_id_hex = random_id(rng)?;
        let vault_id: [u8; 16] = unhex_array(&vault_id_hex).ok_or(VaultError::Rng)?;
        let keys = Keys::derive(Secret32::random(rng)?, &vault_id);
        let recovery = RecoveryKey::generate(rng)?;

        let request = Self::enroll_device(&vault_id_hex, device_label, keystore, rng)?;
        let mut device = empty_slot(random_id(rng)?, KIND_DEVICE, device_label, now, &request.public);
        device.keystore_blob = hex(&request.keystore_blob);
        let mut recovery_entry = recovery_slot(&keys, &vault_id, 1, &recovery, &device.id, rng, now)?;
        wrap_slot(&mut device, &vault_id, 1, &keys.vmk, rng)?;
        wrap_slot(&mut recovery_entry, &vault_id, 1, &keys.vmk, rng)?;

        let mut header = Header {
            format: HEADER_FORMAT.to_string(),
            version: VERSION,
            vault_id: vault_id_hex,
            epoch: 1,
            created: now,
            updated: now,
            slots: vec![device, recovery_entry],
            previous: Vec::new(),
            mac: String::new(),
        };
        header.mac = header.compute_mac(&keys.header);
        header.validate()?;
        store.apply(vec![StoreOp::PutHeader(header.to_bytes())])?;
        let device_slot = header.slots[0].id.clone();
        let vault = Vault {
            store,
            header,
            keys: Some(keys),
            device_slot,
            auto_lock_secs: None,
            last_activity: now,
        };
        Ok((vault, recovery))
    }

    /// A locked vault over a store that holds one. Nothing is authenticated
    /// until it is unlocked.
    pub fn locked(store: S) -> Result<Self, VaultError> {
        let bytes = store.header()?.ok_or(VaultError::NotFound)?;
        let header = Header::parse(&bytes)?;
        Ok(Vault { store, header, keys: None, device_slot: String::new(), auto_lock_secs: None, last_activity: 0 })
    }

    pub fn open_with_device(store: S, keystore: &dyn DeviceKeyStore) -> Result<Self, VaultError> {
        let mut vault = Self::locked(store)?;
        vault.unlock_with_device(keystore)?;
        Ok(vault)
    }

    pub fn open_with_recovery(store: S, recovery: &RecoveryKey) -> Result<Self, VaultError> {
        let mut vault = Self::locked(store)?;
        vault.unlock_with_recovery(recovery)?;
        Ok(vault)
    }

    /// Opens with the 32-byte PRF output of the passkey behind `slot_id`.
    pub fn open_with_prf(store: S, slot_id: &str, prf_output: &Secret32) -> Result<Self, VaultError> {
        let mut vault = Self::locked(store)?;
        vault.unlock_with_prf(slot_id, prf_output)?;
        Ok(vault)
    }

    fn reload_header(&mut self) -> Result<(), VaultError> {
        let bytes = self.store.header()?.ok_or(VaultError::NotFound)?;
        self.header = Header::parse(&bytes)?;
        Ok(())
    }

    pub fn unlock_with_device(&mut self, keystore: &dyn DeviceKeyStore) -> Result<(), VaultError> {
        self.keys = None;
        self.reload_header()?;
        for slot in self.header.slots.iter().filter(|s| s.kind == KIND_DEVICE) {
            let Some(blob) = unhex(&slot.keystore_blob) else { continue };
            let Ok(plain) = keystore.unwrap(&blob) else { continue };
            let Some(secret) = Secret32::from_slice(&plain) else { continue };
            if let Some(keys) = try_slot(&self.header, slot, &secret) {
                self.device_slot = slot.id.clone();
                self.keys = Some(keys);
                return Ok(());
            }
        }
        Err(VaultError::OpenFailed)
    }

    pub fn unlock_with_recovery(&mut self, recovery: &RecoveryKey) -> Result<(), VaultError> {
        self.keys = None;
        self.reload_header()?;
        let keys = self.header.recovery_slot().and_then(|slot| try_slot(&self.header, slot, &recovery.entropy));
        self.device_slot.clear();
        self.keys = Some(keys.ok_or(VaultError::OpenFailed)?);
        Ok(())
    }

    pub fn unlock_with_prf(&mut self, slot_id: &str, prf_output: &Secret32) -> Result<(), VaultError> {
        self.keys = None;
        self.reload_header()?;
        let keys = self
            .header
            .slots
            .iter()
            .find(|s| s.kind == KIND_PRF && s.id == slot_id)
            .and_then(|slot| try_slot(&self.header, slot, prf_output));
        self.device_slot.clear();
        self.keys = Some(keys.ok_or(VaultError::OpenFailed)?);
        Ok(())
    }

    // ------------------------------------------------------------ locking

    /// Drops (and wipes) the master key and everything derived from it.
    pub fn lock(&mut self) {
        self.keys = None;
    }

    pub fn is_locked(&self) -> bool {
        self.keys.is_none()
    }

    /// Lock after this many seconds without [`Vault::note_activity`]; `None` turns it off.
    pub fn set_auto_lock(&mut self, idle_secs: Option<u64>, now: u64) {
        self.auto_lock_secs = idle_secs;
        self.last_activity = now;
    }

    /// The shell calls this when the person does something.
    pub fn note_activity(&mut self, now: u64) {
        self.last_activity = now;
    }

    /// The shell calls this from a timer and when the app goes to the
    /// background. Locks if the idle time has passed; returns whether the
    /// vault is now locked.
    pub fn lock_if_idle(&mut self, now: u64) -> bool {
        if let Some(limit) = self.auto_lock_secs {
            if now.saturating_sub(self.last_activity) >= limit {
                self.lock();
            }
        }
        self.is_locked()
    }

    // ------------------------------------------------------------ facts

    pub fn vault_id(&self) -> &str {
        &self.header.vault_id
    }

    /// How many master keys this vault has had. A device remembers the highest
    /// it has seen and passes it to [`Vault::check_epoch`].
    pub fn epoch(&self) -> u64 {
        self.header.epoch
    }

    /// Refuses a header older than one this device has already seen (someone
    /// put an old header back, for instance one that still lists a lost device).
    pub fn check_epoch(&self, at_least: u64) -> Result<(), VaultError> {
        if self.header.epoch < at_least {
            return Err(VaultError::Rollback);
        }
        Ok(())
    }

    pub fn slots(&self) -> Vec<SlotInfo> {
        self.header
            .slots
            .iter()
            .map(|s| SlotInfo { id: s.id.clone(), kind: s.kind.clone(), label: s.label.clone(), created: s.created })
            .collect()
    }

    pub fn prf_slots(&self) -> Vec<PrfSlotInfo> {
        self.header
            .slots
            .iter()
            .filter(|s| s.kind == KIND_PRF)
            .filter_map(|s| {
                Some(PrfSlotInfo {
                    slot_id: s.id.clone(),
                    label: s.label.clone(),
                    credential_id: unhex(&s.credential_id)?,
                    prf_salt: unhex_array(&s.prf_salt)?,
                })
            })
            .collect()
    }

    pub fn store(&self) -> &S {
        &self.store
    }

    /// Locks and gives the store back.
    pub fn into_store(mut self) -> S {
        self.lock();
        self.store
    }

    fn keys(&self) -> Result<&Keys, VaultError> {
        self.keys.as_ref().ok_or(VaultError::Locked)
    }

    /// A keyed reference to `data` under this vault (HMAC-SHA-256). Receipts
    /// use it to point at a record without revealing or allowing a guess at it.
    pub fn keyed_ref(&self, label: &str, data: &[u8]) -> Result<[u8; 32], VaultError> {
        Ok(keyed_ref(&self.keys()?.reference, label, data))
    }

    // ------------------------------------------------------------ records

    fn open_head_any(&self, id: &str, head: &[u8]) -> Result<(HeadMeta, Option<HeadPlain>), VaultError> {
        let keys = self.keys()?;
        let record_id = record_id_bytes(id)?;
        let meta = head_meta(head).ok_or(VaultError::Tampered)?;
        let key = keys.record_key(meta.epoch, self.header.epoch).ok_or(VaultError::Tampered)?;
        open_head(key, &self.header.vault_id_bytes(), &record_id, head).ok_or(VaultError::Tampered)
    }

    #[allow(clippy::too_many_arguments)]
    fn write_record(
        &mut self,
        id: &str,
        version: u64,
        kind: &str,
        payload: &[u8],
        schema: u32,
        fields: Vec<(String, String)>,
        rng: &mut dyn VaultRng,
    ) -> Result<(), VaultError> {
        if payload.len() > MAX_PAYLOAD {
            return Err(VaultError::TooLarge);
        }
        let keys = self.keys()?;
        let vault_id = self.header.vault_id_bytes();
        let record_id = record_id_bytes(id)?;
        let plain = HeadPlain { data_key: Secret32::random(rng)?, kind: kind.to_string(), fields };
        let meta = HeadMeta { state: STATE_LIVE, version, schema, epoch: self.header.epoch };
        let head = seal_head(&keys.record, &vault_id, &record_id, meta, &random_nonce(rng)?, Some(&plain));
        let body = seal_body(&plain.data_key, &vault_id, &record_id, meta, kind, &random_nonce(rng)?, payload);
        let (kind_tag, index_tags) = tags_for(&keys.index, &plain);
        self.store.put(id, StoredRecord { kind_tag, index_tags, head, body })?;
        Ok(())
    }

    /// Stores a new record and returns its id. `fields` are (name, value)
    /// pairs to be findable by exact match with [`Vault::find`]; index only
    /// what must be looked up, because equal values give equal tags.
    pub fn put(
        &mut self,
        kind: &str,
        payload: &[u8],
        schema: u32,
        fields: &[(&str, &str)],
        rng: &mut dyn VaultRng,
    ) -> Result<String, VaultError> {
        self.keys()?;
        let fields = normalise_fields(kind, fields)?;
        let id = random_id(rng)?;
        self.write_record(&id, 1, kind, payload, schema, fields, rng)?;
        Ok(id)
    }

    /// Replaces a record's content and indexed fields: a new version under a
    /// new record key. Returns the new version number.
    pub fn update(
        &mut self,
        id: &str,
        payload: &[u8],
        schema: u32,
        fields: &[(&str, &str)],
        rng: &mut dyn VaultRng,
    ) -> Result<u64, VaultError> {
        let head = self.store.head(id)?.ok_or(VaultError::NotFound)?;
        let (meta, plain) = self.open_head_any(id, &head)?;
        let plain = plain.ok_or(VaultError::Deleted)?;
        let version = meta.version.checked_add(1).ok_or(VaultError::Invalid("version"))?;
        let kind = plain.kind.clone();
        let fields = normalise_fields(&kind, fields)?;
        self.write_record(id, version, &kind, payload, schema, fields, rng)?;
        Ok(version)
    }

    pub fn get(&self, id: &str) -> Result<Record, VaultError> {
        self.keys()?;
        let stored = self.store.get(id)?.ok_or(VaultError::NotFound)?;
        let (meta, plain) = self.open_head_any(id, &stored.head)?;
        let mut plain = plain.ok_or(VaultError::Deleted)?;
        let payload = open_body(
            &plain.data_key,
            &self.header.vault_id_bytes(),
            &record_id_bytes(id)?,
            meta,
            &plain.kind,
            &stored.body,
        )
        .ok_or(VaultError::Tampered)?;
        Ok(Record {
            id: id.to_string(),
            kind: std::mem::take(&mut plain.kind),
            version: meta.version,
            schema: meta.schema,
            payload,
            fields: std::mem::take(&mut plain.fields),
        })
    }

    /// The ids of the live records of a kind. Every listed head is opened and
    /// checked, so the store cannot file a record under a kind it is not.
    pub fn list(&self, kind: &str) -> Result<Vec<String>, VaultError> {
        let tag = kind_tag(&self.keys()?.index, kind);
        let ids = self.store.list(&tag)?;
        for id in &ids {
            let head = self.store.head(id)?.ok_or(VaultError::Tampered)?;
            match self.open_head_any(id, &head)? {
                (_, Some(plain)) if plain.kind == kind => {}
                _ => return Err(VaultError::Tampered),
            }
        }
        Ok(ids)
    }

    /// The ids of the live records of a kind whose indexed `field` equals
    /// `value` (after normalisation). Exact match only; free-text search is
    /// done by the caller on decrypted records.
    pub fn find(&self, kind: &str, field: &str, value: &str) -> Result<Vec<String>, VaultError> {
        let keys = self.keys()?;
        let wanted = normalise_index_value(value);
        let ids = self.store.find(&kind_tag(&keys.index, kind), &index_tag(&keys.index, kind, field, &wanted))?;
        for id in &ids {
            let head = self.store.head(id)?.ok_or(VaultError::Tampered)?;
            match self.open_head_any(id, &head)? {
                (_, Some(plain))
                    if plain.kind == kind
                        && plain.fields.iter().any(|(n, v)| n == field && ct_eq(v.as_bytes(), wanted.as_bytes())) => {}
                _ => return Err(VaultError::Tampered),
            }
        }
        Ok(ids)
    }

    /// Deletes a record: its head is replaced by an authenticated tombstone and
    /// its body dropped, so the record key no longer exists in this store.
    /// Copies of the old head elsewhere (a backup, a device that has not
    /// synced) still open the old content until they are gone too.
    pub fn delete(&mut self, id: &str, rng: &mut dyn VaultRng) -> Result<(), VaultError> {
        let head = self.store.head(id)?.ok_or(VaultError::NotFound)?;
        let (meta, plain) = self.open_head_any(id, &head)?;
        if plain.is_none() {
            return Ok(());
        }
        let keys = self.keys()?;
        let meta = HeadMeta {
            state: STATE_TOMBSTONE,
            version: meta.version.checked_add(1).ok_or(VaultError::Invalid("version"))?,
            schema: meta.schema,
            epoch: self.header.epoch,
        };
        let head = seal_head(
            &keys.record,
            &self.header.vault_id_bytes(),
            &record_id_bytes(id)?,
            meta,
            &random_nonce(rng)?,
            None,
        );
        self.store.put(id, StoredRecord { kind_tag: String::new(), index_tags: Vec::new(), head, body: Vec::new() })?;
        Ok(())
    }

    /// Removes a tombstone from the store for good (once every device has seen it).
    pub fn purge(&mut self, id: &str) -> Result<(), VaultError> {
        let head = self.store.head(id)?.ok_or(VaultError::NotFound)?;
        match self.open_head_any(id, &head)? {
            (_, None) => Ok(self.store.delete(id)?),
            _ => Err(VaultError::Invalid("record is not deleted")),
        }
    }

    // ------------------------------------------------------------ slots

    fn commit_header(&mut self, mut header: Header, now: u64) -> Result<(), VaultError> {
        header.updated = now;
        header.mac = header.compute_mac(&self.keys()?.header);
        header.validate()?;
        self.store.apply(vec![StoreOp::PutHeader(header.to_bytes())])?;
        self.header = header;
        Ok(())
    }

    /// Run on the *new* device: makes its slot secret, stores it in its own
    /// keystore, and returns the (non-secret) request to send to a device that
    /// is already in the vault. `vault_id` comes from that device during pairing.
    pub fn enroll_device(
        vault_id: &str,
        label: &str,
        keystore: &dyn DeviceKeyStore,
        rng: &mut dyn VaultRng,
    ) -> Result<EnrollRequest, VaultError> {
        check_label(label)?;
        let vault_id: [u8; 16] = unhex_array(vault_id).ok_or(VaultError::Invalid("vault id"))?;
        let secret = Secret32::random(rng)?;
        Ok(EnrollRequest {
            label: label.to_string(),
            public: x25519_public(&slot_private(&secret, &vault_id, KIND_DEVICE)),
            keystore_blob: keystore.wrap(secret.expose())?,
        })
    }

    /// Lets a new device in: wraps the master key to the device's public key
    /// and adds the slot. Returns the slot id. The request must have arrived
    /// over the paired, verified channel: whoever's key is in it can open the vault.
    pub fn add_device_slot(&mut self, request: &EnrollRequest, rng: &mut dyn VaultRng, now: u64) -> Result<String, VaultError> {
        check_label(&request.label)?;
        let keys = self.keys()?;
        if request.keystore_blob.is_empty() || request.keystore_blob.len() > 2048 {
            return Err(VaultError::Invalid("keystore blob"));
        }
        let mut header = self.header.clone();
        if header.slots.len() >= MAX_SLOTS {
            return Err(VaultError::Invalid("too many slots"));
        }
        if header.slots.iter().any(|s| s.kind == KIND_DEVICE && s.label == request.label) {
            return Err(VaultError::Invalid("device label already used"));
        }
        let mut slot = empty_slot(random_id(rng)?, KIND_DEVICE, &request.label, now, &request.public);
        slot.keystore_blob = hex(&request.keystore_blob);
        wrap_slot(&mut slot, &header.vault_id_bytes(), header.epoch, &keys.vmk, rng)?;
        let id = slot.id.clone();
        header.slots.push(slot);
        self.commit_header(header, now)?;
        Ok(id)
    }

    /// 32 fresh bytes to use as the PRF input when registering a passkey slot.
    pub fn new_prf_salt(rng: &mut dyn VaultRng) -> Result<[u8; 32], VaultError> {
        let mut salt = [0u8; 32];
        rng.fill(&mut salt)?;
        Ok(salt)
    }

    /// Adds a passkey as another way in, given the PRF output the passkey
    /// returned for `prf_salt`. Refused until the recovery words have been
    /// confirmed, so a passkey is never the only way in that the person
    /// actually holds.
    pub fn add_prf_slot(
        &mut self,
        label: &str,
        credential_id: &[u8],
        prf_salt: &[u8; 32],
        prf_output: &Secret32,
        rng: &mut dyn VaultRng,
        now: u64,
    ) -> Result<String, VaultError> {
        let keys = self.keys()?;
        if !self.recovery_confirmed() {
            return Err(VaultError::Invalid("recovery key not confirmed"));
        }
        if credential_id.is_empty() || credential_id.len() > 1023 || label.len() > 64 || label.chars().any(char::is_control) {
            return Err(VaultError::Invalid("passkey"));
        }
        let mut header = self.header.clone();
        if header.slots.len() >= MAX_SLOTS {
            return Err(VaultError::Invalid("too many slots"));
        }
        let vault_id = header.vault_id_bytes();
        let public = x25519_public(&slot_private(prf_output, &vault_id, KIND_PRF));
        let mut slot = empty_slot(random_id(rng)?, KIND_PRF, label, now, &public);
        slot.credential_id = hex(credential_id);
        slot.prf_salt = hex(prf_salt);
        wrap_slot(&mut slot, &vault_id, header.epoch, &keys.vmk, rng)?;
        let id = slot.id.clone();
        header.slots.push(slot);
        self.commit_header(header, now)?;
        Ok(id)
    }

    /// Takes a device or passkey slot out of the header. **This alone does not
    /// lock that device out**: it still knows the master key. Use
    /// [`Vault::lost_device`] (remove and rotate) unless the device is being
    /// retired by its owner.
    pub fn remove_device_slot(&mut self, slot_id: &str, now: u64) -> Result<(), VaultError> {
        self.keys()?;
        let mut header = self.header.clone();
        let at = header.slots.iter().position(|s| s.id == slot_id).ok_or(VaultError::NotFound)?;
        if header.slots[at].kind == KIND_RECOVERY {
            return Err(VaultError::Invalid("the recovery slot cannot be removed"));
        }
        header.slots.remove(at);
        self.commit_header(header, now)
    }

    // ------------------------------------------------------------ rotation

    /// A new master key: every remaining slot is re-wrapped, every record head
    /// is re-wrapped and re-indexed, in one atomic batch. Content is not
    /// re-encrypted (each record already has its own key).
    ///
    /// What this cannot do: anything a removed device already copied stays
    /// readable to whoever holds that device and its key.
    pub fn rotate(&mut self, policy: RecoveryPolicy, rng: &mut dyn VaultRng, now: u64) -> Result<Rotation, VaultError> {
        self.rotate_inner(None, policy, rng, now)
    }

    /// "I lost this device": removes its slot and rotates, in one step.
    pub fn lost_device(
        &mut self,
        slot_id: &str,
        policy: RecoveryPolicy,
        rng: &mut dyn VaultRng,
        now: u64,
    ) -> Result<Rotation, VaultError> {
        self.rotate_inner(Some(slot_id), policy, rng, now)
    }

    fn rotate_inner(
        &mut self,
        remove: Option<&str>,
        policy: RecoveryPolicy,
        rng: &mut dyn VaultRng,
        now: u64,
    ) -> Result<Rotation, VaultError> {
        let old = self.keys()?;
        let vault_id = self.header.vault_id_bytes();
        let old_epoch = self.header.epoch;
        let epoch = old_epoch.checked_add(1).ok_or(VaultError::Invalid("epoch"))?;
        let mut header = self.header.clone();
        if let Some(slot_id) = remove {
            let at = header.slots.iter().position(|s| s.id == slot_id).ok_or(VaultError::NotFound)?;
            if header.slots[at].kind == KIND_RECOVERY {
                return Err(VaultError::Invalid("the recovery slot cannot be removed"));
            }
            header.slots.remove(at);
        }
        header.epoch = epoch;
        header.updated = now;
        let mut new = Keys::derive(Secret32::random(rng)?, &vault_id);

        // Recovery: keep the words only if the device that made them is still here.
        let at = header.slots.iter().position(|s| s.kind == KIND_RECOVERY).ok_or(VaultError::Malformed("recovery slot"))?;
        let issuer = header.slots[at].issuer.clone();
        let issuer_present = !issuer.is_empty() && header.slots.iter().any(|s| s.kind == KIND_DEVICE && s.id == issuer);
        let mut new_recovery = None;
        if policy == RecoveryPolicy::Keep && issuer_present {
            let slot = &mut header.slots[at];
            let nonce: [u8; NONCE_LEN] = unhex_array(&slot.check_nonce).ok_or(VaultError::Tampered)?;
            let boxed = unhex(&slot.check_box).ok_or(VaultError::Tampered)?;
            let table = open(&old.words, &nonce, &word_check_aad(&vault_id, &slot.id, old_epoch), &boxed).ok_or(VaultError::Tampered)?;
            let nonce = random_nonce(rng)?;
            slot.check_nonce = hex(&nonce);
            slot.check_box = hex(&seal(&new.words, &nonce, &word_check_aad(&vault_id, &slot.id, epoch), &table));
        } else {
            let recovery = RecoveryKey::generate(rng)?;
            header.slots[at] = recovery_slot(&new, &vault_id, epoch, &recovery, &self.device_slot, rng, now)?;
            new_recovery = Some(recovery);
        }
        for slot in header.slots.iter_mut() {
            wrap_slot(slot, &vault_id, epoch, &new.vmk, rng)?;
        }

        // Retired master keys, sealed under the new one (the newest MAX_PREVIOUS).
        let mut retired: Vec<(u64, Secret32, Secret32)> = old.older.clone();
        retired.push((old_epoch, old.vmk.clone(), old.record.clone()));
        let drop_count = retired.len().saturating_sub(MAX_PREVIOUS);
        retired.drain(..drop_count);
        header.previous = Vec::with_capacity(retired.len());
        for (retired_epoch, vmk, _) in &retired {
            let nonce = random_nonce(rng)?;
            let sealed = seal(&new.previous, &nonce, &previous_aad(&vault_id, *retired_epoch, epoch), vmk.expose());
            header.previous.push(Previous { epoch: *retired_epoch, nonce: hex(&nonce), sealed: hex(&sealed) });
        }
        header.mac = header.compute_mac(&new.header);
        header.validate()?;

        // Heads: open with whichever key sealed them, seal again under the new one.
        let mut ops = vec![StoreOp::PutHeader(header.to_bytes())];
        let (mut rewrapped, mut skipped) = (0, 0);
        for id in self.store.ids()? {
            let opened = match self.store.head(&id)? {
                Some(head) => self.open_head_any(&id, &head).ok(),
                None => None,
            };
            let (Some((mut meta, plain)), Ok(record_id)) = (opened, record_id_bytes(&id)) else {
                skipped += 1;
                continue;
            };
            meta.epoch = epoch;
            let head = seal_head(&new.record, &vault_id, &record_id, meta, &random_nonce(rng)?, plain.as_ref());
            let (kind_tag, index_tags) = match &plain {
                Some(plain) => tags_for(&new.index, plain),
                None => (String::new(), Vec::new()),
            };
            ops.push(StoreOp::PutHead { id, kind_tag, index_tags, head });
            rewrapped += 1;
        }
        self.store.apply(ops)?;
        new.older = retired;
        self.keys = Some(new);
        self.header = header;
        Ok(Rotation { epoch, rewrapped, skipped, new_recovery })
    }

    // ------------------------------------------------------------ recovery words

    /// Whether the person has proved they saved the current recovery words.
    pub fn recovery_confirmed(&self) -> bool {
        self.header.recovery_slot().is_some_and(|s| s.confirmed != 0)
    }

    /// New recovery words without changing the master key (Settings → "make a
    /// new one"). The old words stop opening the vault; they still open
    /// backups made while they were current.
    pub fn renew_recovery(&mut self, rng: &mut dyn VaultRng, now: u64) -> Result<RecoveryKey, VaultError> {
        let keys = self.keys()?;
        let vault_id = self.header.vault_id_bytes();
        let mut header = self.header.clone();
        let at = header.slots.iter().position(|s| s.kind == KIND_RECOVERY).ok_or(VaultError::Malformed("recovery slot"))?;
        let recovery = RecoveryKey::generate(rng)?;
        let mut slot = recovery_slot(keys, &vault_id, header.epoch, &recovery, &self.device_slot, rng, now)?;
        wrap_slot(&mut slot, &vault_id, header.epoch, &keys.vmk, rng)?;
        header.slots[at] = slot;
        self.commit_header(header, now)?;
        Ok(recovery)
    }

    /// Three distinct word positions (1..=24, ascending) to ask the person for.
    pub fn recovery_challenge(rng: &mut dyn VaultRng) -> Result<[u8; 3], VaultError> {
        let mut picked: Vec<u8> = Vec::with_capacity(3);
        while picked.len() < 3 {
            let mut byte = [0u8; 1];
            rng.fill(&mut byte)?;
            // 240 = 10 x 24: reject the rest so every position is equally likely.
            let position = byte[0] % 24 + 1;
            if byte[0] < 240 && !picked.contains(&position) {
                picked.push(position);
            }
        }
        picked.sort_unstable();
        Ok([picked[0], picked[1], picked[2]])
    }

    /// The "re-enter three words" step. `entries` are exactly three
    /// (position, typed word) pairs with distinct positions. Returns whether
    /// all three match; on success the recovery key is marked confirmed.
    ///
    /// The vault does not keep the words. It keeps a 4-bit keyed check value
    /// per position, so a wrong word is accepted with probability 1/16 (three
    /// wrong words: 1/4096). This is a "did you write it down" check, not a
    /// credential.
    pub fn verify_recovery_words(&mut self, language: Language, entries: &[(u8, &str)], now: u64) -> Result<bool, VaultError> {
        let keys = self.keys()?;
        let distinct = entries.len() == 3
            && entries.iter().all(|(p, _)| (1..=24).contains(p))
            && entries[0].0 != entries[1].0
            && entries[0].0 != entries[2].0
            && entries[1].0 != entries[2].0;
        if !distinct {
            return Err(VaultError::Invalid("three distinct word positions are needed"));
        }
        let vault_id = self.header.vault_id_bytes();
        let slot = self.header.recovery_slot().ok_or(VaultError::Malformed("recovery slot"))?;
        let salt: [u8; 16] = unhex_array(&slot.check_salt).ok_or(VaultError::Tampered)?;
        let nonce: [u8; NONCE_LEN] = unhex_array(&slot.check_nonce).ok_or(VaultError::Tampered)?;
        let boxed = unhex(&slot.check_box).ok_or(VaultError::Tampered)?;
        let table = open(&keys.words, &nonce, &word_check_aad(&vault_id, &slot.id, self.header.epoch), &boxed)
            .ok_or(VaultError::Tampered)?;
        let mut all = subtle::Choice::from(1u8);
        for (position, typed) in entries {
            let words = mnemonic::split_words(language, typed);
            let index = if words.len() == 1 { language.index_of(&words[0]) } else { None };
            let i = usize::from(position - 1);
            let stored = if i % 2 == 0 { table[i / 2] >> 4 } else { table[i / 2] & 0x0f };
            // An unknown word is compared against a value no real word can have.
            let got = index.map_or(0xff, |index| primitives::word_check_nibble(&salt, position - 1, index));
            all &= subtle::ConstantTimeEq::ct_eq(&stored, &got);
        }
        let matched = bool::from(all);
        if matched && slot.confirmed == 0 {
            let mut header = self.header.clone();
            if let Some(slot) = header.slots.iter_mut().find(|s| s.kind == KIND_RECOVERY) {
                slot.confirmed = now.max(1);
            }
            self.commit_header(header, now)?;
        }
        Ok(matched)
    }

    // ------------------------------------------------------------ export and import

    /// An age-encrypted archive of the whole vault, readable with the recovery
    /// words alone: the header without device slots, and every record as stored.
    pub fn export(&self, now: u64) -> Result<Vec<u8>, VaultError> {
        let keys = self.keys()?;
        let mut header = self.header.clone();
        header.slots.retain(|s| s.kind != KIND_DEVICE);
        header.mac = header.compute_mac(&keys.header);
        let recipient = header.recovery_slot().ok_or(VaultError::ExportFailed)?.export_recipient.clone();
        let mut entries = Vec::new();
        for id in self.store.ids()? {
            let stored = self.store.get(&id)?.ok_or(VaultError::Store)?;
            entries.push(export::Entry { id, head: stored.head, body: stored.body });
        }
        let plain = Zeroizing::new(export::build(
            &keys.export_mac,
            &header.vault_id,
            header.epoch,
            now,
            &header.to_bytes(),
            &entries,
        )?);
        export::encrypt_to(&recipient, &plain)
    }

    /// Restores an archive into an empty store on a new device. Everything is
    /// decrypted and checked in memory first; the store is written once, at
    /// the end, or not at all.
    pub fn import(
        archive: &[u8],
        recovery: &RecoveryKey,
        mut store: S,
        device_label: &str,
        keystore: &dyn DeviceKeyStore,
        rng: &mut dyn VaultRng,
        now: u64,
    ) -> Result<Self, VaultError> {
        check_label(device_label)?;
        if store.header()?.is_some() || !store.ids()?.is_empty() {
            return Err(VaultError::Exists);
        }
        let failed = VaultError::ImportFailed;
        let plain = export::decrypt_with(&recovery.entropy, archive)?;
        let parsed = export::parse(&plain).ok_or(failed)?;
        let mut header = Header::parse(parsed.header).map_err(|_| failed)?;
        if header.vault_id != parsed.manifest.vault_id || header.epoch != parsed.manifest.epoch {
            return Err(failed);
        }
        let keys = header.recovery_slot().and_then(|slot| try_slot(&header, slot, &recovery.entropy)).ok_or(failed)?;
        if !parsed.verify_mac(&keys.export_mac) {
            return Err(failed);
        }
        let vault_id = header.vault_id_bytes();

        let mut ops = Vec::with_capacity(parsed.entries.len() + 1);
        let mut seen = std::collections::BTreeSet::new();
        for entry in &parsed.entries {
            let record_id: [u8; 16] = unhex_array(&entry.id).ok_or(failed)?;
            if !seen.insert(record_id) {
                return Err(failed);
            }
            let meta = head_meta(&entry.head).ok_or(failed)?;
            let key = keys.record_key(meta.epoch, header.epoch).ok_or(failed)?;
            let (meta, plain) = open_head(key, &vault_id, &record_id, &entry.head).ok_or(failed)?;
            let (kind_tag, index_tags) = match &plain {
                Some(plain) => {
                    open_body(&plain.data_key, &vault_id, &record_id, meta, &plain.kind, &entry.body).ok_or(failed)?;
                    tags_for(&keys.index, plain)
                }
                None if entry.body.is_empty() => (String::new(), Vec::new()),
                None => return Err(failed),
            };
            ops.push(StoreOp::PutRecord {
                id: entry.id.clone(),
                record: StoredRecord { kind_tag, index_tags, head: entry.head.clone(), body: entry.body.clone() },
            });
        }

        let request = Self::enroll_device(&header.vault_id, device_label, keystore, rng)?;
        let mut slot = empty_slot(random_id(rng)?, KIND_DEVICE, device_label, now, &request.public);
        slot.keystore_blob = hex(&request.keystore_blob);
        wrap_slot(&mut slot, &vault_id, header.epoch, &keys.vmk, rng)?;
        let device_slot = slot.id.clone();
        header.slots.insert(0, slot);
        header.updated = now;
        header.mac = header.compute_mac(&keys.header);
        header.validate().map_err(|_| failed)?;
        ops.insert(0, StoreOp::PutHeader(header.to_bytes()));
        store.apply(ops)?;
        Ok(Vault {
            store,
            header,
            keys: Some(keys),
            device_slot,
            auto_lock_secs: None,
            last_activity: now,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// "Crypto-shredding": content is under the record's own key, and that key
    /// exists only inside the head. Once the head is a tombstone, no key the
    /// vault holds (the master key or anything derived from it) opens the old
    /// ciphertext.
    #[test]
    fn after_delete_no_vault_key_opens_the_old_ciphertext() {
        let mut rng = TestRng::new(b"shred");
        let keystore = MemoryKeyStore::new(&mut rng).unwrap();
        let (mut vault, _) = Vault::create(MemoryStore::new(), "laptop", &keystore, &mut rng, 1).unwrap();
        let id = vault.put("memory", b"to be forgotten", 1, &[], &mut rng).unwrap();
        let old = vault.store().records[&id].clone();
        vault.delete(&id, &mut rng).unwrap();

        let vault_id = vault.header.vault_id_bytes();
        let record_id = record_id_bytes(&id).unwrap();
        let meta = HeadMeta { state: STATE_LIVE, version: 1, schema: 1, epoch: 1 };
        let keys = vault.keys.as_ref().unwrap();
        let candidates =
            [&keys.vmk, &keys.record, &keys.index, &keys.reference, &keys.header, &keys.words, &keys.previous, &keys.export_mac];
        for key in candidates {
            assert!(open_body(key, &vault_id, &record_id, meta, "memory", &old.body).is_none());
        }
        // The key that does open it is only in the old head, which the store no longer has.
        let (_, plain) = open_head(&keys.record, &vault_id, &record_id, &old.head).unwrap();
        let plain = plain.unwrap();
        assert!(open_body(&plain.data_key, &vault_id, &record_id, meta, "memory", &old.body).is_some());
        assert!(!vault.store().records[&id].head.windows(32).any(|w| w == plain.data_key.expose()));
    }

    #[test]
    fn locking_drops_every_key() {
        let mut rng = TestRng::new(b"lock");
        let keystore = MemoryKeyStore::new(&mut rng).unwrap();
        let (mut vault, _) = Vault::create(MemoryStore::new(), "laptop", &keystore, &mut rng, 1).unwrap();
        assert!(vault.keys.is_some());
        vault.lock();
        assert!(vault.keys.is_none(), "Keys is made of Secret32 values, which wipe themselves when dropped");
    }

    #[test]
    fn purpose_keys_are_all_different() {
        let keys = Keys::derive(Secret32::new([1; 32]), &[2; 16]);
        let all = [&keys.vmk, &keys.record, &keys.index, &keys.reference, &keys.header, &keys.words, &keys.previous, &keys.export_mac];
        for (i, a) in all.iter().enumerate() {
            for b in &all[i + 1..] {
                assert!(a != b);
            }
        }
        let other_vault = Keys::derive(Secret32::new([1; 32]), &[3; 16]);
        assert!(keys.record != other_vault.record, "the vault id is the HKDF salt");
    }
}
