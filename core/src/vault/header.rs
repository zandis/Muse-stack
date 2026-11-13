//! The wrap-slots file ("header"): who can open the vault, and the bytes that
//! are authenticated. Nothing in it is secret; all of it is authenticated.

use serde::{Deserialize, Serialize};

use super::primitives::hmac256;
use super::secret::{ct_eq, hex, unhex, unhex_array, Secret32};
use super::VaultError;

pub const HEADER_FORMAT: &str = "muse-vault-header";
/// The only header version this code reads or writes.
pub const VERSION: u32 = 1;
pub const KIND_DEVICE: &str = "device";
pub const KIND_RECOVERY: &str = "recovery";
pub const KIND_PRF: &str = "prf";
pub const MAX_SLOTS: usize = 64;
/// How many retired master keys the header keeps (sealed under the current one).
pub const MAX_PREVIOUS: usize = 8;

/// The `kdf` text for a slot kind: how the slot's 32-byte secret is obtained
/// and turned into its X25519 key.
pub fn kdf_for(kind: &str) -> Option<&'static str> {
    match kind {
        KIND_DEVICE => Some("keystore+hkdf-sha256"),
        KIND_RECOVERY => Some("bip39-entropy+hkdf-sha256"),
        KIND_PRF => Some("webauthn-prf+hkdf-sha256"),
        _ => None,
    }
}

/// One way in. Every field is always present; a field that does not apply to
/// the slot's kind is the empty string (or 0).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Slot {
    /// 16 random bytes, hex.
    pub id: String,
    /// `device`, `recovery` or `prf`.
    pub kind: String,
    /// The device's name for a device slot; a display name for a passkey; empty for recovery.
    pub label: String,
    /// Unix seconds.
    pub created: u64,
    pub kdf: String,
    /// The slot's X25519 public key, hex.
    pub public: String,
    /// The ephemeral X25519 public key of the current wrap, hex.
    pub eph: String,
    /// 24-byte nonce of the current wrap, hex.
    pub nonce: String,
    /// The master key, wrapped (32 bytes + 16-byte tag), hex.
    pub wrapped: String,
    /// device: what the device's keystore returned for the slot secret, hex.
    pub keystore_blob: String,
    /// prf: the WebAuthn credential id, hex.
    pub credential_id: String,
    /// prf: the 32-byte PRF input for this slot, hex.
    pub prf_salt: String,
    /// recovery: the id of the device slot that created this recovery key.
    pub issuer: String,
    /// recovery: when the person proved they saved the words (Unix seconds); 0 = not yet.
    pub confirmed: u64,
    /// recovery: the age recipient that exports are encrypted to.
    pub export_recipient: String,
    /// recovery: salt of the word check values, hex (16 bytes).
    pub check_salt: String,
    /// recovery: nonce of `check_box`, hex.
    pub check_nonce: String,
    /// recovery: the 24 four-bit word check values, encrypted, hex (12 bytes + tag).
    pub check_box: String,
}

/// A retired master key, sealed under the current one, so records written
/// under an older key on a device that had not yet seen a rotation stay readable.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Previous {
    pub epoch: u64,
    pub nonce: String,
    pub sealed: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Header {
    pub format: String,
    pub version: u32,
    /// 16 random bytes, hex.
    pub vault_id: String,
    /// Counts master keys: 1 at creation, +1 at each rotation.
    pub epoch: u64,
    pub created: u64,
    pub updated: u64,
    pub slots: Vec<Slot>,
    pub previous: Vec<Previous>,
    /// HMAC-SHA-256 over [`Header::transcript`] under the header key, hex.
    pub mac: String,
}

fn lp(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u32).to_be_bytes());
    out.extend_from_slice(bytes);
}

fn is_label(text: &str, allow_empty: bool) -> bool {
    (allow_empty || !text.is_empty()) && text.len() <= 64 && !text.chars().any(char::is_control)
}

fn hex_len(text: &str, bytes: usize) -> bool {
    text.len() == bytes * 2 && unhex(text).is_some()
}

impl Slot {
    /// The associated data of the slot's wrap: everything that fixes what the
    /// slot is, so a wrap cannot be moved to another slot, vault, epoch or version.
    pub fn aad(&self, vault_id: &[u8; 16], epoch: u64) -> Vec<u8> {
        let mut aad = b"muse-vault/v1/slot".to_vec();
        aad.extend_from_slice(&VERSION.to_be_bytes());
        aad.extend_from_slice(vault_id);
        aad.extend_from_slice(&epoch.to_be_bytes());
        lp(&mut aad, &unhex(&self.id).unwrap_or_default());
        lp(&mut aad, self.kind.as_bytes());
        lp(&mut aad, self.kdf.as_bytes());
        lp(&mut aad, &unhex(&self.public).unwrap_or_default());
        aad
    }

    fn validate(&self) -> Result<(), VaultError> {
        let bad = Err(VaultError::Malformed("slot"));
        if !hex_len(&self.id, 16)
            || kdf_for(&self.kind) != Some(self.kdf.as_str())
            || !hex_len(&self.public, 32)
            || !hex_len(&self.eph, 32)
            || !hex_len(&self.nonce, 24)
            || !hex_len(&self.wrapped, 48)
        {
            return bad;
        }
        let empty = |fields: &[&String]| fields.iter().all(|f| f.is_empty());
        let ok = match self.kind.as_str() {
            KIND_DEVICE => {
                is_label(&self.label, false)
                    && !self.keystore_blob.is_empty()
                    && self.keystore_blob.len() <= 4096
                    && unhex(&self.keystore_blob).is_some()
                    && self.confirmed == 0
                    && empty(&[
                        &self.credential_id,
                        &self.prf_salt,
                        &self.issuer,
                        &self.export_recipient,
                        &self.check_salt,
                        &self.check_nonce,
                        &self.check_box,
                    ])
            }
            KIND_PRF => {
                is_label(&self.label, true)
                    && !self.credential_id.is_empty()
                    && self.credential_id.len() <= 2046
                    && unhex(&self.credential_id).is_some()
                    && hex_len(&self.prf_salt, 32)
                    && self.confirmed == 0
                    && empty(&[
                        &self.keystore_blob,
                        &self.issuer,
                        &self.export_recipient,
                        &self.check_salt,
                        &self.check_nonce,
                        &self.check_box,
                    ])
            }
            KIND_RECOVERY => {
                self.label.is_empty()
                    && is_label(&self.issuer, true)
                    && self.export_recipient.starts_with("age1")
                    && self.export_recipient.len() <= 128
                    && self.export_recipient.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
                    && hex_len(&self.check_salt, 16)
                    && hex_len(&self.check_nonce, 24)
                    && hex_len(&self.check_box, 28)
                    && empty(&[&self.keystore_blob, &self.credential_id, &self.prf_salt])
            }
            _ => false,
        };
        if ok {
            Ok(())
        } else {
            bad
        }
    }
}

impl Header {
    /// Reads and checks the shape of a header. This does not authenticate it:
    /// that needs the master key (see [`Header::verify_mac`]).
    pub fn parse(bytes: &[u8]) -> Result<Header, VaultError> {
        let header: Header = serde_json::from_slice(bytes).map_err(|_| VaultError::Malformed("header"))?;
        header.validate()?;
        Ok(header)
    }

    pub fn validate(&self) -> Result<(), VaultError> {
        if self.format != HEADER_FORMAT {
            return Err(VaultError::Malformed("header format"));
        }
        if self.version != VERSION {
            return Err(VaultError::UnsupportedVersion);
        }
        if !hex_len(&self.vault_id, 16) || self.epoch == 0 || !hex_len(&self.mac, 32) {
            return Err(VaultError::Malformed("header"));
        }
        if self.slots.is_empty() || self.slots.len() > MAX_SLOTS {
            return Err(VaultError::Malformed("slots"));
        }
        for (i, slot) in self.slots.iter().enumerate() {
            slot.validate()?;
            let clash = self.slots[..i]
                .iter()
                .any(|other| other.id == slot.id || (slot.kind == KIND_DEVICE && other.kind == KIND_DEVICE && other.label == slot.label));
            if clash {
                return Err(VaultError::Malformed("slots"));
            }
        }
        // The recovery key is always a way in, so a passkey can never be the only one.
        if self.slots.iter().filter(|s| s.kind == KIND_RECOVERY).count() != 1 {
            return Err(VaultError::Malformed("recovery slot"));
        }
        if self.previous.len() > MAX_PREVIOUS {
            return Err(VaultError::Malformed("previous"));
        }
        let mut last = 0;
        for entry in &self.previous {
            if entry.epoch <= last || entry.epoch >= self.epoch || !hex_len(&entry.nonce, 24) || !hex_len(&entry.sealed, 48) {
                return Err(VaultError::Malformed("previous"));
            }
            last = entry.epoch;
        }
        Ok(())
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        serde_json::to_vec(self).expect("a header is plain data")
    }

    pub fn vault_id_bytes(&self) -> [u8; 16] {
        unhex_array(&self.vault_id).unwrap_or([0u8; 16])
    }

    pub fn recovery_slot(&self) -> Option<&Slot> {
        self.slots.iter().find(|s| s.kind == KIND_RECOVERY)
    }

    /// The exact bytes the header MAC covers: every field except `mac`, in a
    /// fixed order, each variable-length field preceded by its length
    /// (`u32` big-endian), integers as 8 bytes big-endian (`version` as 4).
    /// Hex fields are covered as text, so nothing depends on JSON formatting.
    pub fn transcript(&self) -> Vec<u8> {
        let mut t = b"muse-vault/v1/header".to_vec();
        t.extend_from_slice(&self.version.to_be_bytes());
        lp(&mut t, self.format.as_bytes());
        lp(&mut t, self.vault_id.as_bytes());
        t.extend_from_slice(&self.epoch.to_be_bytes());
        t.extend_from_slice(&self.created.to_be_bytes());
        t.extend_from_slice(&self.updated.to_be_bytes());
        t.extend_from_slice(&(self.slots.len() as u32).to_be_bytes());
        for s in &self.slots {
            for field in [&s.id, &s.kind, &s.label] {
                lp(&mut t, field.as_bytes());
            }
            t.extend_from_slice(&s.created.to_be_bytes());
            for field in [&s.kdf, &s.public, &s.eph, &s.nonce, &s.wrapped, &s.keystore_blob, &s.credential_id, &s.prf_salt, &s.issuer] {
                lp(&mut t, field.as_bytes());
            }
            t.extend_from_slice(&s.confirmed.to_be_bytes());
            for field in [&s.export_recipient, &s.check_salt, &s.check_nonce, &s.check_box] {
                lp(&mut t, field.as_bytes());
            }
        }
        t.extend_from_slice(&(self.previous.len() as u32).to_be_bytes());
        for p in &self.previous {
            t.extend_from_slice(&p.epoch.to_be_bytes());
            lp(&mut t, p.nonce.as_bytes());
            lp(&mut t, p.sealed.as_bytes());
        }
        t
    }

    pub fn compute_mac(&self, k_header: &Secret32) -> String {
        hex(&hmac256(k_header.expose(), &[&self.transcript()]))
    }

    pub fn verify_mac(&self, k_header: &Secret32) -> bool {
        ct_eq(self.compute_mac(k_header).as_bytes(), self.mac.as_bytes())
    }
}
