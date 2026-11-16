//! Secret containers, randomness and small encoding helpers.

use std::fmt;

use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use zeroize::{Zeroize, ZeroizeOnDrop};

use super::VaultError;

/// 32 secret bytes. Wiped when dropped; never printed.
#[derive(Clone, Zeroize, ZeroizeOnDrop)]
pub struct Secret32([u8; 32]);

impl Secret32 {
    pub fn new(bytes: [u8; 32]) -> Self {
        Secret32(bytes)
    }

    /// Copies `bytes` (which must be 32 long); the caller wipes its own copy.
    pub fn from_slice(bytes: &[u8]) -> Option<Self> {
        let array: [u8; 32] = bytes.try_into().ok()?;
        Some(Secret32(array))
    }

    pub fn random(rng: &mut dyn VaultRng) -> Result<Self, VaultError> {
        let mut out = Secret32([0u8; 32]);
        rng.fill(&mut out.0)?;
        Ok(out)
    }

    /// The bytes, for handing to a primitive. Do not copy them anywhere that is not wiped.
    pub fn expose(&self) -> &[u8; 32] {
        &self.0
    }
}

impl fmt::Debug for Secret32 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret32(<redacted>)")
    }
}

impl PartialEq for Secret32 {
    fn eq(&self, other: &Self) -> bool {
        ct_eq(&self.0, &other.0)
    }
}

impl Eq for Secret32 {}

/// Constant-time equality (length is not hidden).
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    a.ct_eq(b).into()
}

/// Where random bytes come from. Injected so tests and vectors are deterministic.
pub trait VaultRng {
    fn fill(&mut self, dest: &mut [u8]) -> Result<(), VaultError>;
}

/// The operating system's generator (through the `getrandom` crate).
#[derive(Debug, Default, Clone, Copy)]
pub struct OsRng;

impl VaultRng for OsRng {
    fn fill(&mut self, dest: &mut [u8]) -> Result<(), VaultError> {
        getrandom::getrandom(dest).map_err(|_| VaultError::Rng)
    }
}

/// A fixed, predictable byte stream: block `i` is `SHA-256(seed || u64_be(i))`.
/// **For tests and conformance vectors only.** A vault made with it has no secrecy.
#[derive(Clone)]
pub struct TestRng {
    seed: Vec<u8>,
    counter: u64,
    buffer: Vec<u8>,
}

impl TestRng {
    pub fn new(seed: &[u8]) -> Self {
        TestRng { seed: seed.to_vec(), counter: 0, buffer: Vec::new() }
    }
}

impl fmt::Debug for TestRng {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("TestRng(<test only>)")
    }
}

impl VaultRng for TestRng {
    fn fill(&mut self, dest: &mut [u8]) -> Result<(), VaultError> {
        for byte in dest.iter_mut() {
            if self.buffer.is_empty() {
                let mut hasher = Sha256::new();
                hasher.update(&self.seed);
                hasher.update(self.counter.to_be_bytes());
                self.counter += 1;
                self.buffer = hasher.finalize().to_vec();
                self.buffer.reverse(); // pop() then yields the block front to back
            }
            *byte = self.buffer.pop().unwrap_or(0);
        }
        Ok(())
    }
}

const HEX: &[u8; 16] = b"0123456789abcdef";

/// Lowercase hex.
pub fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 15) as usize] as char);
    }
    out
}

/// Strict lowercase hex; `None` for anything else (uppercase, odd length, other characters).
pub fn unhex(text: &str) -> Option<Vec<u8>> {
    let raw = text.as_bytes();
    if raw.len() & 1 == 1 {
        return None;
    }
    let nibble = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    };
    raw.chunks(2).map(|pair| Some(nibble(pair[0])? << 4 | nibble(pair[1])?)).collect()
}

/// Hex of exactly `N` bytes.
pub fn unhex_array<const N: usize>(text: &str) -> Option<[u8; N]> {
    unhex(text)?.try_into().ok()
}

/// A fresh random identifier: 16 bytes as 32 hex characters.
pub fn random_id(rng: &mut dyn VaultRng) -> Result<String, VaultError> {
    let mut raw = [0u8; 16];
    rng.fill(&mut raw)?;
    Ok(hex(&raw))
}

/// SHA-256 as lowercase hex.
pub fn sha256_hex(data: &[u8]) -> String {
    hex(&Sha256::digest(data))
}
