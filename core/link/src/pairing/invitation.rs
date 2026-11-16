//! The invitation: what the QR code carries.
//!
//! ```text
//! payload = version (1 byte, = 1)
//!         | expires (u32, big-endian, Unix seconds)
//!         | inviter link id (32 bytes)
//!         | one-time secret (16 bytes)
//!         | relay: length (1 byte, 0..=120) then that many bytes of URL
//!         | count (1 byte, 0..=4) then per address: 4 or 6 (1 byte), the IP (4 or 16 bytes), port (u16)
//! text    = "muse://pair?c=" + base64url(payload), no padding
//! ```
//!
//! It holds an address and a secret that is useless after one attempt or
//! after it expires. It never holds a vault key.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

use muse_core::vault::primitives::hkdf32;
use zeroize::Zeroizing;

use crate::endpoint::LinkAddr;
use crate::frame::Cursor;

pub const INVITATION_VERSION: u8 = 1;
pub const SECRET_LEN: usize = 16;
pub const URL_PREFIX: &str = "muse://pair?c=";
pub const MAX_RELAY_URL: usize = 120;
pub const MAX_DIRECT_ADDRS: usize = 4;
/// The longest text an invitation can be: fits a version 15 QR code at error correction M.
pub const MAX_QR_TEXT: usize = 400;

pub(crate) const KDF_SALT: &[u8] = b"muse/pair/1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvitationError {
    /// Not text made by `to_qr_string`.
    Malformed,
    /// A version this build does not speak. Nothing older is accepted either.
    Version,
    /// Too long to be an invitation.
    TooLong,
}

impl std::fmt::Display for InvitationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            InvitationError::Malformed => "this is not a Muse pairing code",
            InvitationError::Version => "this pairing code was made by a different version of Muse",
            InvitationError::TooLong => "this is too long to be a pairing code",
        })
    }
}

impl std::error::Error for InvitationError {}

#[derive(Clone, PartialEq, Eq)]
pub struct Invitation {
    pub addr: LinkAddr,
    /// Unix seconds; after this neither side continues.
    pub expires: u64,
    pub(crate) secret: Zeroizing<[u8; SECRET_LEN]>,
}

impl std::fmt::Debug for Invitation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Invitation").field("addr", &self.addr).field("expires", &self.expires).finish_non_exhaustive()
    }
}

impl Invitation {
    pub(crate) fn new(mut addr: LinkAddr, expires: u64, secret: [u8; SECRET_LEN]) -> Invitation {
        addr.direct.truncate(MAX_DIRECT_ADDRS);
        if addr.relay.as_ref().is_some_and(|r| r.len() > MAX_RELAY_URL) {
            addr.relay = None;
        }
        Invitation { addr, expires: expires.min(u32::MAX as u64), secret: Zeroizing::new(secret) }
    }

    /// Names the invitation to the inviter without revealing the secret.
    pub fn id(&self) -> [u8; 8] {
        let mut id = [0u8; 8];
        id.copy_from_slice(&hkdf32(KDF_SALT, &self.secret[..], b"invite-id").expose()[..8]);
        id
    }

    /// The Noise pre-shared key.
    pub(crate) fn psk(&self) -> Zeroizing<[u8; 32]> {
        Zeroizing::new(*hkdf32(KDF_SALT, &self.secret[..], b"psk").expose())
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(128);
        out.push(INVITATION_VERSION);
        out.extend_from_slice(&(self.expires as u32).to_be_bytes());
        out.extend_from_slice(&self.addr.id);
        out.extend_from_slice(&self.secret[..]);
        let relay = self.addr.relay.as_deref().unwrap_or("");
        out.push(relay.len() as u8);
        out.extend_from_slice(relay.as_bytes());
        out.push(self.addr.direct.len() as u8);
        for addr in &self.addr.direct {
            match addr.ip() {
                IpAddr::V4(ip) => {
                    out.push(4);
                    out.extend_from_slice(&ip.octets());
                }
                IpAddr::V6(ip) => {
                    out.push(6);
                    out.extend_from_slice(&ip.octets());
                }
            }
            out.extend_from_slice(&addr.port().to_be_bytes());
        }
        out
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Invitation, InvitationError> {
        let bad = |_| InvitationError::Malformed;
        let mut c = Cursor::new(bytes);
        if c.u8().map_err(bad)? != INVITATION_VERSION {
            return Err(InvitationError::Version);
        }
        let expires = c.u32().map_err(bad)? as u64;
        let id: [u8; 32] = c.array().map_err(bad)?;
        let secret: [u8; SECRET_LEN] = c.array().map_err(bad)?;
        let relay = Cursor::text(c.lp8(MAX_RELAY_URL).map_err(bad)?).map_err(bad)?;
        if !relay.bytes().all(|b| b.is_ascii_graphic()) {
            return Err(InvitationError::Malformed);
        }
        let count = c.u8().map_err(bad)? as usize;
        if count > MAX_DIRECT_ADDRS {
            return Err(InvitationError::Malformed);
        }
        let mut direct = Vec::with_capacity(count);
        for _ in 0..count {
            let ip = match c.u8().map_err(bad)? {
                4 => IpAddr::V4(Ipv4Addr::from(c.array::<4>().map_err(bad)?)),
                6 => IpAddr::V6(Ipv6Addr::from(c.array::<16>().map_err(bad)?)),
                _ => return Err(InvitationError::Malformed),
            };
            direct.push(SocketAddr::new(ip, c.u16().map_err(bad)?));
        }
        c.end().map_err(bad)?;
        let relay = if relay.is_empty() { None } else { Some(relay.to_string()) };
        Ok(Invitation { addr: LinkAddr { id, relay, direct }, expires, secret: Zeroizing::new(secret) })
    }

    /// The text for the QR code, also usable as a deep link.
    pub fn to_qr_string(&self) -> String {
        format!("{URL_PREFIX}{}", base64url_encode(&self.to_bytes()))
    }

    pub fn from_qr_string(text: &str) -> Result<Invitation, InvitationError> {
        let text = text.trim();
        if text.len() > MAX_QR_TEXT {
            return Err(InvitationError::TooLong);
        }
        let code = text.strip_prefix(URL_PREFIX).ok_or(InvitationError::Malformed)?;
        Invitation::from_bytes(&base64url_decode(code).ok_or(InvitationError::Malformed)?)
    }

    /// For tests: the same invitation as an attacker who has everything but the secret would write it.
    #[cfg(feature = "test-util")]
    pub fn with_secret_for_tests(&self, secret: [u8; SECRET_LEN]) -> Invitation {
        Invitation { addr: self.addr.clone(), expires: self.expires, secret: Zeroizing::new(secret) }
    }

    /// For tests: the same secret and expiry, pointing at another device.
    #[cfg(feature = "test-util")]
    pub fn with_addr_for_tests(&self, addr: LinkAddr) -> Invitation {
        Invitation { addr, expires: self.expires, secret: self.secret.clone() }
    }
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

pub fn base64url_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16) | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8) | u32::from(*chunk.get(2).unwrap_or(&0));
        for i in 0..=chunk.len() {
            out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

/// Strict: the URL-safe alphabet only, no padding, no spare bits set.
pub fn base64url_decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if bytes.len() % 4 == 1 {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3 + 2);
    for chunk in bytes.chunks(4) {
        let mut n = 0u32;
        for (i, b) in chunk.iter().enumerate() {
            let v = B64.iter().position(|c| c == b)? as u32;
            n |= v << (18 - 6 * i);
        }
        let produced = chunk.len() - 1;
        if n & ((1u32 << (24 - 8 * produced)) - 1) != 0 {
            return None;
        }
        out.extend_from_slice(&n.to_be_bytes()[1..1 + produced]);
    }
    Some(out)
}

/// The smallest QR version (1 to 20) that holds `len` bytes in byte mode at
/// error correction level M (ISO/IEC 18004, table 7). `None`: needs more than version 20.
pub fn qr_version_m(len: usize) -> Option<u8> {
    const CAPACITY_M: [usize; 20] = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412, 450, 504, 560, 624, 666];
    CAPACITY_M.iter().position(|cap| len <= *cap).map(|i| i as u8 + 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(relay: Option<&str>, direct: Vec<SocketAddr>) -> Invitation {
        Invitation::new(LinkAddr { id: [7; 32], relay: relay.map(str::to_string), direct }, 1_800_000_000, [9; 16])
    }

    #[test]
    fn base64url_round_trip_and_strictness() {
        for len in 0..70usize {
            let bytes: Vec<u8> = (0..len).map(|i| (i * 37 + len) as u8).collect();
            let text = base64url_encode(&bytes);
            assert!(!text.contains('='));
            assert_eq!(base64url_decode(&text).unwrap(), bytes);
        }
        assert_eq!(base64url_decode("A"), None);
        assert_eq!(base64url_decode("AB"), None); // spare bits set
        assert_eq!(base64url_decode("AA=="), None);
        assert_eq!(base64url_decode("A+A/"), None);
        assert_eq!(base64url_decode("AA"), Some(vec![0]));
    }

    #[test]
    fn round_trip_with_every_shape_of_address() {
        let v4: SocketAddr = "192.168.1.20:51000".parse().unwrap();
        let v6: SocketAddr = "[2001:db8::1]:443".parse().unwrap();
        for inv in [
            sample(None, vec![]),
            sample(Some("https://use1-1.relay.n0.iroh.link./"), vec![]),
            sample(Some("https://use1-1.relay.n0.iroh.link./"), vec![v4, v6]),
            sample(None, vec![v4, v4, v6, v6]),
        ] {
            let text = inv.to_qr_string();
            assert!(text.starts_with("muse://pair?c="));
            let back = Invitation::from_qr_string(&text).unwrap();
            assert_eq!(back, inv);
            assert_eq!(back.id(), inv.id());
            assert_eq!(Invitation::from_qr_string(&format!("  {text}\n")).unwrap(), inv);
        }
    }

    #[test]
    fn size_is_bounded_and_fits_a_modest_qr_code() {
        let v4: SocketAddr = "192.168.1.20:51000".parse().unwrap();
        let v6: SocketAddr = "[2001:db8::1]:443".parse().unwrap();
        // Typical: one relay, one IPv4 and one IPv6 address.
        let typical = sample(Some("https://use1-1.relay.n0.iroh.link./"), vec![v4, v6]);
        assert_eq!(typical.to_bytes().len(), 116);
        assert_eq!(typical.to_qr_string().len(), 169);
        assert_eq!(qr_version_m(typical.to_qr_string().len()), Some(9));
        // Relay only (address lookup or relay does the rest).
        let small = sample(Some("https://use1-1.relay.n0.iroh.link./"), vec![]);
        assert_eq!(small.to_qr_string().len(), 134);
        assert_eq!(qr_version_m(small.to_qr_string().len()), Some(8));
        // The largest an invitation can be.
        let relay = format!("https://{}/", "r".repeat(MAX_RELAY_URL - 9));
        let largest = sample(Some(&relay), vec![v6, v6, v6, v6]);
        let text = largest.to_qr_string();
        assert_eq!(largest.to_bytes().len(), 1 + 4 + 32 + 16 + 1 + 120 + 1 + 4 * 19);
        assert!(text.len() <= MAX_QR_TEXT, "{}", text.len());
        assert_eq!(qr_version_m(text.len()), Some(14));
        // More addresses than fit are dropped, not encoded.
        let many = sample(None, vec![v4; 9]);
        assert_eq!(many.addr.direct.len(), MAX_DIRECT_ADDRS);
    }

    #[test]
    fn the_text_never_holds_anything_but_address_secret_and_expiry() {
        let inv = sample(Some("https://relay.example/"), vec![]);
        // 1 + 4 + 32 + 16 + 1 + relay + 1: every byte is accounted for.
        assert_eq!(inv.to_bytes().len(), 55 + "https://relay.example/".len());
    }

    #[test]
    fn rejects_other_versions_other_prefixes_and_damage() {
        let inv = sample(Some("https://relay.example/"), vec!["10.0.0.1:1".parse().unwrap()]);
        let bytes = inv.to_bytes();
        for version in [0u8, 2, 255] {
            let mut other = bytes.clone();
            other[0] = version;
            assert_eq!(Invitation::from_bytes(&other), Err(InvitationError::Version));
        }
        for cut in 0..bytes.len() {
            assert!(Invitation::from_bytes(&bytes[..cut]).is_err(), "cut at {cut}");
        }
        let mut extra = bytes.clone();
        extra.push(0);
        assert_eq!(Invitation::from_bytes(&extra), Err(InvitationError::Malformed));
        let text = inv.to_qr_string();
        for bad in [text.replace("muse://", "http://"), text.replace("pair?c=", "pair?d="), format!("{text}="), format!("{text}&x=1"), "muse://pair?c=".to_string(), String::new()] {
            assert!(Invitation::from_qr_string(&bad).is_err(), "{bad}");
        }
        assert_eq!(Invitation::from_qr_string(&format!("{URL_PREFIX}{}", "A".repeat(500))), Err(InvitationError::TooLong));
        // A changed byte either fails to parse or is a different invitation; it is never the same one.
        for at in 0..bytes.len() {
            let mut changed = bytes.clone();
            changed[at] ^= 0x01;
            if let Ok(other) = Invitation::from_bytes(&changed) {
                assert_ne!(other, inv);
            }
        }
    }

    #[test]
    fn id_and_psk_are_different_derivations_of_the_secret() {
        let a = sample(None, vec![]);
        let b = Invitation::new(LinkAddr::new([7; 32]), 1, [8; 16]);
        assert_ne!(a.id(), b.id());
        assert_ne!(a.psk()[..], b.psk()[..]);
        assert_ne!(a.id()[..], a.psk()[..8]);
        assert!(!format!("{a:?}").contains("secret"));
    }
}
