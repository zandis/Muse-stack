//! The export archive: an age file (X25519 recipient derived from the recovery
//! key) around a small container with a manifest, the header without device
//! slots, and every record as stored.

use bech32::{Bech32, Hrp};
use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

use super::primitives::{export_identity_bytes, hmac256};
use super::secret::{ct_eq, sha256_hex, Secret32};
use super::VaultError;

pub const ARCHIVE_MAGIC: &[u8; 4] = b"MVX1";
pub const EXPORT_FORMAT: &str = "muse-vault-export";
pub const EXPORT_VERSION: u32 = 1;
const MAC_LEN: usize = 32;

/// The age identity (secret) that opens exports made for a recovery key.
fn identity(recovery_entropy: &Secret32) -> Result<age::x25519::Identity, VaultError> {
    let raw = export_identity_bytes(recovery_entropy);
    let hrp = Hrp::parse("age-secret-key-").map_err(|_| VaultError::ExportFailed)?;
    let text = Zeroizing::new(bech32::encode_upper::<Bech32>(hrp, raw.expose()).map_err(|_| VaultError::ExportFailed)?);
    text.parse::<age::x25519::Identity>().map_err(|_| VaultError::ExportFailed)
}

/// The age recipient (public, `age1...`) for a recovery key.
pub fn recipient_for(recovery_entropy: &Secret32) -> Result<String, VaultError> {
    Ok(identity(recovery_entropy)?.to_public().to_string())
}

pub fn encrypt_to(recipient: &str, plain: &[u8]) -> Result<Vec<u8>, VaultError> {
    let recipient: age::x25519::Recipient = recipient.parse().map_err(|_| VaultError::ExportFailed)?;
    age::encrypt(&recipient, plain).map_err(|_| VaultError::ExportFailed)
}

pub fn decrypt_with(recovery_entropy: &Secret32, archive: &[u8]) -> Result<Zeroizing<Vec<u8>>, VaultError> {
    let identity = identity(recovery_entropy).map_err(|_| VaultError::ImportFailed)?;
    age::decrypt(&identity, archive).map(Zeroizing::new).map_err(|_| VaultError::ImportFailed)
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ManifestRecord {
    pub id: String,
    pub head_len: u64,
    pub body_len: u64,
    /// SHA-256 of `head || body`, hex.
    pub sha256: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub format: String,
    pub version: u32,
    pub vault_id: String,
    pub epoch: u64,
    /// Unix seconds.
    pub created: u64,
    pub header_len: u64,
    pub header_sha256: String,
    pub records: Vec<ManifestRecord>,
}

/// One record inside an archive.
pub struct Entry {
    pub id: String,
    pub head: Vec<u8>,
    pub body: Vec<u8>,
}

/// `"MVX1" || u32_be(manifest_len) || manifest || header || (head || body)* || mac(32)`,
/// where `mac = HMAC-SHA-256(K_export_mac, everything before it)`.
pub fn build(
    k_export_mac: &Secret32,
    vault_id: &str,
    epoch: u64,
    created: u64,
    header: &[u8],
    entries: &[Entry],
) -> Result<Vec<u8>, VaultError> {
    let manifest = Manifest {
        format: EXPORT_FORMAT.to_string(),
        version: EXPORT_VERSION,
        vault_id: vault_id.to_string(),
        epoch,
        created,
        header_len: header.len() as u64,
        header_sha256: sha256_hex(header),
        records: entries
            .iter()
            .map(|e| ManifestRecord {
                id: e.id.clone(),
                head_len: e.head.len() as u64,
                body_len: e.body.len() as u64,
                sha256: sha256_hex(&[e.head.as_slice(), e.body.as_slice()].concat()),
            })
            .collect(),
    };
    let manifest = serde_json::to_vec(&manifest).map_err(|_| VaultError::ExportFailed)?;
    let mut out = ARCHIVE_MAGIC.to_vec();
    out.extend_from_slice(&(manifest.len() as u32).to_be_bytes());
    out.extend_from_slice(&manifest);
    out.extend_from_slice(header);
    for entry in entries {
        out.extend_from_slice(&entry.head);
        out.extend_from_slice(&entry.body);
    }
    let mac = hmac256(k_export_mac.expose(), &[&out]);
    out.extend_from_slice(&mac);
    Ok(out)
}

/// A parsed archive whose hashes match its manifest. The MAC is checked
/// separately ([`Parsed::verify_mac`]) because the key comes from the header inside.
pub struct Parsed<'a> {
    pub manifest: Manifest,
    pub header: &'a [u8],
    pub entries: Vec<Entry>,
    signed: &'a [u8],
    mac: &'a [u8],
}

impl Parsed<'_> {
    pub fn verify_mac(&self, k_export_mac: &Secret32) -> bool {
        ct_eq(&hmac256(k_export_mac.expose(), &[self.signed]), self.mac)
    }
}

pub fn parse(plain: &[u8]) -> Option<Parsed<'_>> {
    if plain.len() < 8 + MAC_LEN || &plain[..4] != ARCHIVE_MAGIC {
        return None;
    }
    let (signed, mac) = plain.split_at(plain.len() - MAC_LEN);
    let manifest_len = u32::from_be_bytes(signed[4..8].try_into().ok()?) as usize;
    let mut rest = &signed[8..];
    let take = |rest: &mut &[u8], n: u64| -> Option<Vec<u8>> {
        let n = usize::try_from(n).ok()?;
        if rest.len() < n {
            return None;
        }
        let (front, back) = rest.split_at(n);
        *rest = back;
        Some(front.to_vec())
    };
    let manifest_bytes = take(&mut rest, manifest_len as u64)?;
    let manifest: Manifest = serde_json::from_slice(&manifest_bytes).ok()?;
    if manifest.format != EXPORT_FORMAT || manifest.version != EXPORT_VERSION {
        return None;
    }
    let header_len = usize::try_from(manifest.header_len).ok()?;
    if rest.len() < header_len {
        return None;
    }
    let (header, mut rest) = rest.split_at(header_len);
    if sha256_hex(header) != manifest.header_sha256 {
        return None;
    }
    let mut entries = Vec::with_capacity(manifest.records.len());
    for record in &manifest.records {
        let head = take(&mut rest, record.head_len)?;
        let body = take(&mut rest, record.body_len)?;
        if sha256_hex(&[head.as_slice(), body.as_slice()].concat()) != record.sha256 {
            return None;
        }
        entries.push(Entry { id: record.id.clone(), head, body });
    }
    if !rest.is_empty() {
        return None;
    }
    Some(Parsed { manifest, header, entries, signed, mac })
}
