//! Who may use the Link: the paired devices of one person.
//!
//! A device has two keys (docs/spec/interfaces/link-and-pairing.md section 3):
//!
//! - its **device id**, the Ed25519 key that signs its records (record-format
//!   section 1). It can live in hardware and is used only to sign.
//! - its **link key**, the Ed25519 key of its iroh endpoint. It must be in the
//!   memory of the networking process whenever the device is reachable.
//!
//! The device id vouches for the link key with a signature (the *link
//! certificate*), checked once at pairing and kept in the registry. The Link
//! authenticates the link key; the registry maps it to the device, its role
//! and whether it is still paired.

use std::collections::BTreeMap;
use std::sync::Mutex;

use muse_core::records::{from_hex, to_hex, verify_signature, Signer};

pub type DeviceId = [u8; 32];
pub type LinkId = [u8; 32];

const CERT_DOMAIN: &[u8] = b"muse/link-cert/1\0";

/// What a device may do on the home, in the home API's own words
/// (docs/spec/01-architecture.md section 4.1). The Link adds no token scheme:
/// the role stored here is the one the home's `verify(token) -> Device`
/// returns today for a device token, found by key instead of by token.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    /// The person's own app or command line: every scope.
    Owner,
    /// A host bot's tools server: only what it needs, only what it made.
    Host,
    /// Reads only.
    Viewer,
}

impl Role {
    pub fn name(self) -> &'static str {
        match self {
            Role::Owner => "owner",
            Role::Host => "host",
            Role::Viewer => "viewer",
        }
    }

    pub fn from_name(name: &str) -> Option<Role> {
        [Role::Owner, Role::Host, Role::Viewer].into_iter().find(|r| r.name() == name)
    }

    pub(crate) fn to_u8(self) -> u8 {
        match self {
            Role::Owner => 1,
            Role::Host => 2,
            Role::Viewer => 3,
        }
    }

    pub(crate) fn from_u8(byte: u8) -> Option<Role> {
        [Role::Owner, Role::Host, Role::Viewer].into_iter().find(|r| r.to_u8() == byte)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DeviceState {
    Active,
    /// Removed or lost. Final: the same keys are never let back in.
    Removed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceEntry {
    pub device_id: DeviceId,
    pub link_id: LinkId,
    /// The device's name, as in its vault slot (1 to 64 bytes).
    pub label: String,
    pub role: Role,
    pub state: DeviceState,
    /// The device id's signature over the link key ([`link_cert_message`]).
    pub cert: [u8; 64],
    /// Unix seconds.
    pub paired_at: u64,
    /// The device's slot in the vault header (hex), so "I lost this device" finds it.
    pub slot_id: String,
}

impl DeviceEntry {
    /// The entry a device writes for itself when it creates the vault.
    pub fn for_self(signer: &dyn Signer, link_id: &LinkId, label: &str, role: Role, slot_id: &str, now: u64) -> Option<DeviceEntry> {
        Some(DeviceEntry {
            device_id: device_id_of(signer)?,
            link_id: *link_id,
            label: label.to_string(),
            role,
            state: DeviceState::Active,
            cert: certify(signer, link_id)?,
            paired_at: now,
            slot_id: slot_id.to_string(),
        })
    }

    pub fn device_hex(&self) -> String {
        to_hex(&self.device_id)
    }

    pub fn cert_ok(&self) -> bool {
        verify_link_cert(&self.device_id, &self.link_id, &self.cert)
    }
}

/// The signer's device id as bytes.
pub fn device_id_of(signer: &dyn Signer) -> Option<DeviceId> {
    from_hex(&signer.device())?.try_into().ok()
}

/// What a device id signs to vouch for a link key.
pub fn link_cert_message(device_id: &DeviceId, link_id: &LinkId) -> Vec<u8> {
    [CERT_DOMAIN, device_id, link_id].concat()
}

/// The link certificate: `signer` (the device id) vouches for `link_id`.
pub fn certify(signer: &dyn Signer, link_id: &LinkId) -> Option<[u8; 64]> {
    let device_id = device_id_of(signer)?;
    Some(signer.sign(&link_cert_message(&device_id, link_id)))
}

pub fn verify_link_cert(device_id: &DeviceId, link_id: &LinkId, cert: &[u8; 64]) -> bool {
    verify_signature(&to_hex(device_id), &link_cert_message(device_id, link_id), &to_hex(cert))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RegistryError {
    /// The device or its link key was removed earlier; removal is final.
    Removed,
    /// The link key or device id belongs to another entry.
    Conflict,
    /// The link certificate does not verify.
    BadCertificate,
    NotFound,
    /// The store behind the registry failed.
    Store(String),
}

impl std::fmt::Display for RegistryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RegistryError::Removed => f.write_str("this device was removed and cannot be added again"),
            RegistryError::Conflict => f.write_str("the key belongs to another device"),
            RegistryError::BadCertificate => f.write_str("the link certificate does not verify"),
            RegistryError::NotFound => f.write_str("no such device"),
            RegistryError::Store(e) => write!(f, "registry store: {e}"),
        }
    }
}

impl std::error::Error for RegistryError {}

/// Where the list of paired devices is kept. The vault will hold it (a later
/// package); [`MemoryRegistry`] is for tests and for a shell that persists it
/// itself. Calls are short and synchronous: the accept path asks on every
/// connection and every request.
pub trait DeviceRegistry: Send + Sync {
    fn by_link(&self, link_id: &LinkId) -> Option<DeviceEntry>;
    fn by_device(&self, device_id: &DeviceId) -> Option<DeviceEntry>;
    /// Every entry, removed ones included, in device id order.
    fn list(&self) -> Vec<DeviceEntry>;
    /// Adds an active device. Refused when the certificate is bad, when either
    /// key is already known under another device, or when it was removed.
    /// Adding the identical active entry again is not an error.
    fn add(&self, entry: DeviceEntry) -> Result<(), RegistryError>;
    /// Marks a device removed. Final.
    fn remove(&self, device_id: &DeviceId) -> Result<DeviceEntry, RegistryError>;
}

/// May this link key use the application protocol right now?
pub fn active_peer(registry: &dyn DeviceRegistry, link_id: &LinkId) -> Option<DeviceEntry> {
    registry.by_link(link_id).filter(|e| e.state == DeviceState::Active)
}

#[derive(Debug, Default)]
pub struct MemoryRegistry {
    entries: Mutex<BTreeMap<DeviceId, DeviceEntry>>,
}

impl MemoryRegistry {
    pub fn new() -> MemoryRegistry {
        MemoryRegistry::default()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, BTreeMap<DeviceId, DeviceEntry>> {
        self.entries.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl DeviceRegistry for MemoryRegistry {
    fn by_link(&self, link_id: &LinkId) -> Option<DeviceEntry> {
        self.lock().values().find(|e| &e.link_id == link_id).cloned()
    }

    fn by_device(&self, device_id: &DeviceId) -> Option<DeviceEntry> {
        self.lock().get(device_id).cloned()
    }

    fn list(&self) -> Vec<DeviceEntry> {
        self.lock().values().cloned().collect()
    }

    fn add(&self, entry: DeviceEntry) -> Result<(), RegistryError> {
        if entry.state != DeviceState::Active {
            return Err(RegistryError::Removed);
        }
        if !entry.cert_ok() {
            return Err(RegistryError::BadCertificate);
        }
        let mut entries = self.lock();
        if let Some(existing) = entries.get(&entry.device_id) {
            if existing.state == DeviceState::Removed {
                return Err(RegistryError::Removed);
            }
            return if *existing == entry { Ok(()) } else { Err(RegistryError::Conflict) };
        }
        if let Some(other) = entries.values().find(|e| e.link_id == entry.link_id) {
            return Err(if other.state == DeviceState::Removed { RegistryError::Removed } else { RegistryError::Conflict });
        }
        entries.insert(entry.device_id, entry);
        Ok(())
    }

    fn remove(&self, device_id: &DeviceId) -> Result<DeviceEntry, RegistryError> {
        let mut entries = self.lock();
        let entry = entries.get_mut(device_id).ok_or(RegistryError::NotFound)?;
        entry.state = DeviceState::Removed;
        Ok(entry.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use muse_core::records::SeedSigner;

    fn entry(seed: u8, link: u8) -> DeviceEntry {
        DeviceEntry::for_self(&SeedSigner::new(&[seed; 32]), &[link; 32], "phone", Role::Owner, "00", 1).unwrap()
    }

    #[test]
    fn certificate_binds_device_and_link_key() {
        let e = entry(1, 9);
        assert!(e.cert_ok());
        let mut other_link = e.clone();
        other_link.link_id = [8; 32];
        assert!(!other_link.cert_ok());
        let mut other_device = e.clone();
        other_device.device_id = device_id_of(&SeedSigner::new(&[2; 32])).unwrap();
        assert!(!other_device.cert_ok());
        for at in 0..64 {
            let mut bad = e.clone();
            bad.cert[at] ^= 1;
            assert!(!bad.cert_ok());
        }
    }

    #[test]
    fn removal_is_final_and_keys_are_unique() {
        let reg = MemoryRegistry::new();
        let a = entry(1, 9);
        reg.add(a.clone()).unwrap();
        reg.add(a.clone()).unwrap();
        assert!(active_peer(&reg, &a.link_id).is_some());
        // Another device may not claim the same link key, nor the same device another link key.
        assert_eq!(reg.add(entry(2, 9)), Err(RegistryError::Conflict));
        assert_eq!(reg.add(entry(1, 7)), Err(RegistryError::Conflict));
        let mut forged = entry(3, 5);
        forged.cert = a.cert;
        assert_eq!(reg.add(forged), Err(RegistryError::BadCertificate));
        assert_eq!(reg.remove(&a.device_id).unwrap().state, DeviceState::Removed);
        assert!(active_peer(&reg, &a.link_id).is_none());
        assert_eq!(reg.add(a.clone()), Err(RegistryError::Removed));
        assert_eq!(reg.add(entry(2, 9)), Err(RegistryError::Removed));
        assert_eq!(reg.remove(&[0; 32]), Err(RegistryError::NotFound));
        assert_eq!(reg.list().len(), 1);
    }

    #[test]
    fn role_names_are_the_home_api_names() {
        for role in [Role::Owner, Role::Host, Role::Viewer] {
            assert_eq!(Role::from_name(role.name()), Some(role));
            assert_eq!(Role::from_u8(role.to_u8()), Some(role));
        }
        assert_eq!(Role::from_name("admin"), None);
    }
}
