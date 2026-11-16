//! Shared by the integration tests: devices on 127.0.0.1 with relays and
//! address lookup off, so nothing here needs or touches the internet.
#![allow(dead_code)]

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use muse_core::records::SeedSigner;
use muse_link::registry::{DeviceEntry, DeviceRegistry, MemoryRegistry, Role};
use muse_link::{Clock, LinkConfig, LinkEndpoint, LinkKey};

pub struct Device {
    pub seed: [u8; 32],
    pub registry: Arc<MemoryRegistry>,
    pub endpoint: LinkEndpoint,
    pub label: String,
}

impl Device {
    pub fn signer(&self) -> SeedSigner {
        SeedSigner::new(&self.seed)
    }

    /// The registry entry this device would have (role as given).
    pub fn entry(&self, role: Role) -> DeviceEntry {
        DeviceEntry::for_self(&self.signer(), &self.endpoint.id(), &self.label, role, "", 1).unwrap()
    }

    pub fn device_id(&self) -> [u8; 32] {
        self.entry(Role::Owner).device_id
    }
}

/// A device whose id key is `[n; 32]` and whose link key is `[n + 100; 32]`.
pub async fn device(n: u8, config: LinkConfig) -> Device {
    let registry = Arc::new(MemoryRegistry::new());
    let key = LinkKey::from_bytes([n.wrapping_add(100); 32]);
    let endpoint = LinkEndpoint::bind(&key, config, registry.clone()).await.expect("bind on loopback");
    Device { seed: [n; 32], registry, endpoint, label: format!("device-{n}") }
}

/// `server` now knows `client` as a paired device with `role`.
pub fn admit(server: &Device, client: &Device, role: Role) {
    server.registry.add(client.entry(role)).unwrap();
}

/// A clock the test moves by hand (Unix seconds).
pub fn manual_clock(start: u64) -> (Clock, Arc<AtomicU64>) {
    let now = Arc::new(AtomicU64::new(start));
    let reader = now.clone();
    (Arc::new(move || reader.load(Ordering::SeqCst)), now)
}

pub fn offline() -> LinkConfig {
    LinkConfig::offline()
}
