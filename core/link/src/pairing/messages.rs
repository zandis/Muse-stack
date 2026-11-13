//! The messages sent inside the pairing session, after the handshake.
//!
//! Each is one type byte and a fixed layout. None has a field that could hold
//! a vault key: the largest secret-looking things are a public key, a
//! keystore blob (useless without the new device's keystore) and the vault
//! header (public by design: vault-format section 16).

use crate::frame::{put_lp16, put_lp32, put_lp8, Cursor, FrameError};
use crate::registry::{DeviceEntry, DeviceState, Role};

pub const MAX_HEADER_BYTES: usize = 512 * 1024;
pub const MAX_ENTRIES: usize = 64;
pub const MAX_MANDATE_BYTES: usize = 16 * 1024;
pub const MAX_LABEL: usize = 64;
pub const MAX_KEYSTORE_BLOB: usize = 2048;

/// Why a pairing stopped. Sent to the other side so both can say why.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum AbortReason {
    /// No open invitation with that name (or it was already used).
    UnknownInvite = 1,
    Expired = 2,
    /// The handshake failed: the two sides do not share the secret, or are
    /// not talking over the same connection.
    Handshake = 3,
    /// The person said the words differ.
    Mismatch = 4,
    /// The person, or the app, cancelled.
    Cancelled = 5,
    /// The other side sent something out of order or malformed.
    Protocol = 6,
    /// The inviting device could not add the new device (for example, the name is taken).
    Refused = 7,
    /// The new device could not use what it was sent.
    Unusable = 8,
}

impl AbortReason {
    pub fn from_u8(byte: u8) -> AbortReason {
        use AbortReason::*;
        [UnknownInvite, Expired, Handshake, Mismatch, Cancelled, Protocol, Refused, Unusable]
            .into_iter()
            .find(|r| *r as u8 == byte)
            .unwrap_or(AbortReason::Protocol)
    }
}

/// What the new device sends once both people-facing confirmations are in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Enroll {
    pub label: String,
    /// The X25519 public key for the device's vault slot.
    pub slot_public: [u8; 32],
    /// What the new device's keystore returned for its slot secret.
    pub keystore_blob: Vec<u8>,
    /// The device id: the Ed25519 key that signs its records.
    pub device_id: [u8; 32],
    /// The device id's signature over the link key this connection authenticated.
    pub link_cert: [u8; 64],
}

/// What the inviting device sends back: everything the new device needs to
/// open the vault with its own slot, and nothing secret.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bootstrap {
    pub vault_id: [u8; 16],
    pub epoch: u64,
    /// The new device's slot id (hex).
    pub slot_id: String,
    /// The vault header, now holding the new device's slot.
    pub header: Vec<u8>,
    /// The active devices, the inviter and the new device included.
    pub devices: Vec<DeviceEntry>,
    /// The new device's own mandate token, when the inviter issued one.
    pub mandate: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Msg {
    /// Joiner: its 32-byte value for the check words.
    Nonce([u8; 32]),
    /// Inviter: the value it committed to during the handshake.
    Reveal([u8; 32]),
    /// The person confirmed the words on this side.
    Confirm,
    Abort(AbortReason),
    VaultInfo { vault_id: [u8; 16] },
    Enroll(Enroll),
    Bootstrap(Box<Bootstrap>),
    /// Joiner: the header was stored and the vault opens.
    Done,
}

fn put_entry(out: &mut Vec<u8>, e: &DeviceEntry) {
    out.extend_from_slice(&e.device_id);
    out.extend_from_slice(&e.link_id);
    out.push(e.role.to_u8());
    put_lp8(out, e.label.as_bytes());
    out.extend_from_slice(&e.cert);
    out.extend_from_slice(&e.paired_at.to_be_bytes());
    put_lp8(out, e.slot_id.as_bytes());
}

fn take_entry(c: &mut Cursor<'_>) -> Result<DeviceEntry, FrameError> {
    let device_id = c.array()?;
    let link_id = c.array()?;
    let role = Role::from_u8(c.u8()?).ok_or(FrameError::Malformed("role"))?;
    let label = Cursor::text(c.lp8(MAX_LABEL)?)?.to_string();
    let cert = c.array()?;
    let paired_at = c.u64()?;
    let slot_id = Cursor::text(c.lp8(64)?)?.to_string();
    // Only active devices are ever sent; there is no way to write another state.
    Ok(DeviceEntry { device_id, link_id, label, role, state: DeviceState::Active, cert, paired_at, slot_id })
}

impl Msg {
    pub fn name(&self) -> &'static str {
        match self {
            Msg::Nonce(_) => "nonce",
            Msg::Reveal(_) => "reveal",
            Msg::Confirm => "confirm",
            Msg::Abort(_) => "abort",
            Msg::VaultInfo { .. } => "vault-info",
            Msg::Enroll(_) => "enroll",
            Msg::Bootstrap(_) => "bootstrap",
            Msg::Done => "done",
        }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        match self {
            Msg::Nonce(n) => {
                out.push(1);
                out.extend_from_slice(n);
            }
            Msg::Reveal(n) => {
                out.push(2);
                out.extend_from_slice(n);
            }
            Msg::Confirm => out.push(3),
            Msg::Abort(reason) => {
                out.push(4);
                out.push(*reason as u8);
            }
            Msg::VaultInfo { vault_id } => {
                out.push(5);
                out.extend_from_slice(vault_id);
            }
            Msg::Enroll(e) => {
                out.push(6);
                put_lp8(&mut out, e.label.as_bytes());
                out.extend_from_slice(&e.slot_public);
                put_lp16(&mut out, &e.keystore_blob);
                out.extend_from_slice(&e.device_id);
                out.extend_from_slice(&e.link_cert);
            }
            Msg::Bootstrap(b) => {
                out.push(7);
                out.extend_from_slice(&b.vault_id);
                out.extend_from_slice(&b.epoch.to_be_bytes());
                put_lp8(&mut out, b.slot_id.as_bytes());
                put_lp32(&mut out, &b.header);
                out.extend_from_slice(&(b.devices.len() as u16).to_be_bytes());
                for e in &b.devices {
                    put_entry(&mut out, e);
                }
                match &b.mandate {
                    None => out.push(0),
                    Some(token) => {
                        out.push(1);
                        put_lp16(&mut out, token.as_bytes());
                    }
                }
            }
            Msg::Done => out.push(8),
        }
        out
    }

    pub fn decode(bytes: &[u8]) -> Result<Msg, FrameError> {
        let mut c = Cursor::new(bytes);
        let msg = match c.u8()? {
            1 => Msg::Nonce(c.array()?),
            2 => Msg::Reveal(c.array()?),
            3 => Msg::Confirm,
            4 => Msg::Abort(AbortReason::from_u8(c.u8()?)),
            5 => Msg::VaultInfo { vault_id: c.array()? },
            6 => {
                let label = Cursor::text(c.lp8(MAX_LABEL)?)?.to_string();
                let slot_public = c.array()?;
                let keystore_blob = c.lp16(MAX_KEYSTORE_BLOB)?.to_vec();
                let device_id = c.array()?;
                let link_cert = c.array()?;
                Msg::Enroll(Enroll { label, slot_public, keystore_blob, device_id, link_cert })
            }
            7 => {
                let vault_id = c.array()?;
                let epoch = c.u64()?;
                let slot_id = Cursor::text(c.lp8(64)?)?.to_string();
                let header = c.lp32(MAX_HEADER_BYTES)?.to_vec();
                let count = c.u16()? as usize;
                if count > MAX_ENTRIES {
                    return Err(FrameError::Malformed("too many devices"));
                }
                let mut devices = Vec::with_capacity(count);
                for _ in 0..count {
                    devices.push(take_entry(&mut c)?);
                }
                let mandate = match c.u8()? {
                    0 => None,
                    1 => Some(Cursor::text(c.lp16(MAX_MANDATE_BYTES)?)?.to_string()),
                    _ => return Err(FrameError::Malformed("mandate marker")),
                };
                Msg::Bootstrap(Box::new(Bootstrap { vault_id, epoch, slot_id, header, devices, mandate }))
            }
            8 => Msg::Done,
            _ => return Err(FrameError::Malformed("message type")),
        };
        c.end()?;
        Ok(msg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use muse_core::records::SeedSigner;

    fn samples() -> Vec<Msg> {
        let entry = DeviceEntry::for_self(&SeedSigner::new(&[1; 32]), &[2; 32], "desk", Role::Owner, "aa", 5).unwrap();
        vec![
            Msg::Nonce([1; 32]),
            Msg::Reveal([2; 32]),
            Msg::Confirm,
            Msg::Abort(AbortReason::Mismatch),
            Msg::VaultInfo { vault_id: [3; 16] },
            Msg::Enroll(Enroll { label: "phone".into(), slot_public: [4; 32], keystore_blob: vec![5; 72], device_id: [6; 32], link_cert: [7; 64] }),
            Msg::Bootstrap(Box::new(Bootstrap {
                vault_id: [8; 16],
                epoch: 3,
                slot_id: "ab".repeat(16),
                header: b"{\"format\":\"muse-vault-header\"}".to_vec(),
                devices: vec![entry.clone(), entry],
                mandate: Some("token".into()),
            })),
            Msg::Done,
        ]
    }

    #[test]
    fn round_trip_truncation_and_trailing_bytes() {
        for msg in samples() {
            let bytes = msg.encode();
            assert_eq!(Msg::decode(&bytes).unwrap(), msg);
            for cut in 0..bytes.len() {
                assert!(Msg::decode(&bytes[..cut]).is_err(), "{} cut at {cut}", msg.name());
            }
            let mut extra = bytes.clone();
            extra.push(0);
            assert!(Msg::decode(&extra).is_err());
        }
        assert!(Msg::decode(&[0]).is_err());
        assert!(Msg::decode(&[9]).is_err());
        assert_eq!(Msg::decode(&[4, 200]).unwrap(), Msg::Abort(AbortReason::Protocol));
    }

    #[test]
    fn random_bytes_never_panic() {
        let mut state = 0x243F_6A88_85A3_08D3u64;
        for len in 0..300usize {
            let mut bytes = Vec::with_capacity(len);
            for _ in 0..len {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                bytes.push((state >> 33) as u8);
            }
            for kind in 0..10u8 {
                if let Some(first) = bytes.first_mut() {
                    *first = kind;
                }
                let _ = Msg::decode(&bytes);
            }
        }
    }
}
