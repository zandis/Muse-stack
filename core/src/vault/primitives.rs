//! The format's building blocks, each a pure function of its arguments.
//!
//! These take keys as arguments so that the conformance vectors
//! (`core/conformance/rust/vault.json`) can pin every byte, and so that a second
//! implementation can be checked piece by piece. Application code uses
//! [`super::Vault`], which never hands a key out.
//!
//! Byte layouts are specified in `docs/spec/interfaces/vault-format.md`.

use argon2::{Algorithm, Argon2, Params, Version};
use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{Key, XChaCha20Poly1305, XNonce};
use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;
use x25519_dalek::{PublicKey, StaticSecret};
use zeroize::{Zeroize, Zeroizing};

use super::secret::{hex, Secret32};

/// Length of the Poly1305 tag appended to every ciphertext.
pub const TAG_LEN: usize = 16;
/// XChaCha20-Poly1305 nonce length.
pub const NONCE_LEN: usize = 24;
/// Largest record payload accepted (64 MiB). Larger things are files, not records.
pub const MAX_PAYLOAD: usize = 64 * 1024 * 1024;

/// HKDF-SHA-256 to 32 bytes.
pub fn hkdf32(salt: &[u8], ikm: &[u8], info: &[u8]) -> Secret32 {
    let mut okm = [0u8; 32];
    Hkdf::<Sha256>::new(Some(salt), ikm)
        .expand(info, &mut okm)
        .expect("32 bytes is a valid HKDF-SHA-256 output length");
    let out = Secret32::new(okm);
    okm.zeroize();
    out
}

/// A purpose key under the vault master key:
/// `HKDF-SHA-256(salt = vault_id, ikm = VMK, info = "muse-vault/v1/key/" || purpose)`.
pub fn subkey(vmk: &Secret32, vault_id: &[u8; 16], purpose: &str) -> Secret32 {
    let mut info = b"muse-vault/v1/key/".to_vec();
    info.extend_from_slice(purpose.as_bytes());
    hkdf32(vault_id, vmk.expose(), &info)
}

/// HMAC-SHA-256 over the concatenation of `parts`.
pub fn hmac256(key: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(key).expect("HMAC takes a key of any length");
    for part in parts {
        mac.update(part);
    }
    mac.finalize().into_bytes().into()
}

/// XChaCha20-Poly1305: ciphertext followed by the 16-byte tag.
pub fn seal(key: &Secret32, nonce: &[u8; NONCE_LEN], aad: &[u8], plaintext: &[u8]) -> Vec<u8> {
    XChaCha20Poly1305::new(Key::from_slice(key.expose()))
        .encrypt(XNonce::from_slice(nonce), Payload { msg: plaintext, aad })
        .expect("a record is far below the cipher's length limit")
}

/// The inverse of [`seal`]; `None` when the key, nonce, associated data or any byte is wrong.
pub fn open(key: &Secret32, nonce: &[u8; NONCE_LEN], aad: &[u8], boxed: &[u8]) -> Option<Zeroizing<Vec<u8>>> {
    XChaCha20Poly1305::new(Key::from_slice(key.expose()))
        .decrypt(XNonce::from_slice(nonce), Payload { msg: boxed, aad })
        .ok()
        .map(Zeroizing::new)
}

// ---------------------------------------------------------------- wrap slots

/// The X25519 private key of a slot, from the slot's 32-byte secret (what the
/// device keystore returned, the recovery entropy, or the passkey PRF output):
/// `HKDF-SHA-256(salt = vault_id, ikm = secret, info = "muse-vault/v1/slot-x25519/" || kind)`.
pub fn slot_private(secret: &Secret32, vault_id: &[u8; 16], kind: &str) -> Secret32 {
    let mut info = b"muse-vault/v1/slot-x25519/".to_vec();
    info.extend_from_slice(kind.as_bytes());
    hkdf32(vault_id, secret.expose(), &info)
}

/// The X25519 public key for a private key (clamped as in RFC 7748).
pub fn x25519_public(private: &Secret32) -> [u8; 32] {
    PublicKey::from(&StaticSecret::from(*private.expose())).to_bytes()
}

fn slot_kek(shared: &[u8; 32], eph_public: &[u8; 32], slot_public: &[u8; 32]) -> Secret32 {
    let mut salt = [0u8; 64];
    salt[..32].copy_from_slice(eph_public);
    salt[32..].copy_from_slice(slot_public);
    hkdf32(&salt, shared, b"muse-vault/v1/slot-kek")
}

/// Wraps the master key to a slot's public key. `eph` is 32 fresh random bytes
/// used once as an X25519 private key. Returns the ephemeral public key and the
/// 48-byte box, or `None` if `slot_public` is a low-order point.
pub fn wrap_to_slot(
    eph: &Secret32,
    nonce: &[u8; NONCE_LEN],
    slot_public: &[u8; 32],
    aad: &[u8],
    vmk: &Secret32,
) -> Option<([u8; 32], Vec<u8>)> {
    let eph_secret = StaticSecret::from(*eph.expose());
    let eph_public = PublicKey::from(&eph_secret).to_bytes();
    let shared = eph_secret.diffie_hellman(&PublicKey::from(*slot_public));
    if !shared.was_contributory() {
        return None;
    }
    let kek = slot_kek(shared.as_bytes(), &eph_public, slot_public);
    Some((eph_public, seal(&kek, nonce, aad, vmk.expose())))
}

/// Unwraps the master key with a slot's private key; `None` on any failure.
pub fn unwrap_from_slot(
    private: &Secret32,
    eph_public: &[u8; 32],
    nonce: &[u8; NONCE_LEN],
    aad: &[u8],
    wrapped: &[u8],
) -> Option<Secret32> {
    let secret = StaticSecret::from(*private.expose());
    let slot_public = PublicKey::from(&secret).to_bytes();
    let shared = secret.diffie_hellman(&PublicKey::from(*eph_public));
    if !shared.was_contributory() {
        return None;
    }
    let kek = slot_kek(shared.as_bytes(), eph_public, &slot_public);
    let plain = open(&kek, nonce, aad, wrapped)?;
    Secret32::from_slice(&plain)
}

// ---------------------------------------------------------------- padding

/// The padded length for `n` bytes: powers of two from 256 bytes to 64 KiB,
/// then multiples of 64 KiB.
pub fn bucket_size(n: usize) -> usize {
    let mut size = 256;
    while size < n && size < 65536 {
        size *= 2;
    }
    if n <= size {
        size
    } else {
        n.div_ceil(65536) * 65536
    }
}

/// `u32_be(len) || payload || zeros` up to the bucket.
pub fn pad_payload(payload: &[u8]) -> Zeroizing<Vec<u8>> {
    let mut out = Zeroizing::new(Vec::with_capacity(bucket_size(payload.len() + 4)));
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.extend_from_slice(payload);
    let target = bucket_size(out.len());
    out.resize(target, 0);
    out
}

/// The inverse of [`pad_payload`]. `None` if the length field or the padding is not as written.
pub fn unpad_payload(padded: &[u8]) -> Option<Zeroizing<Vec<u8>>> {
    let len = u32::from_be_bytes(padded.get(..4)?.try_into().ok()?) as usize;
    let end = len.checked_add(4)?;
    if padded.len() != bucket_size(end) || padded[end..].iter().any(|&b| b != 0) {
        return None;
    }
    Some(Zeroizing::new(padded[4..end].to_vec()))
}

/// The padded length of a record head's plaintext: a multiple of 64, at least 128.
pub fn head_bucket(n: usize) -> usize {
    n.max(1).div_ceil(64).max(2) * 64
}

// ---------------------------------------------------------------- blind index

/// How an indexed value is normalised before it is hashed, so that the same
/// text typed two ways finds the same record: NFKC, lowercase, NFKC again,
/// then leading and trailing white space removed.
pub fn normalise_index_value(value: &str) -> String {
    let folded: String = value.nfkc().collect::<String>().to_lowercase();
    folded.nfkc().collect::<String>().trim().to_string()
}

fn lp16(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u16).to_be_bytes());
    out.extend_from_slice(bytes);
}

/// The blind index tag of one field: the first 16 bytes, in hex, of
/// `HMAC-SHA-256(K_index, "muse-vault/v1/index\0" || lp16(kind) || lp16(field) || lp16(value))`.
/// `value` must already be normalised.
pub fn index_tag(k_index: &Secret32, kind: &str, field: &str, value: &str) -> String {
    let mut message = b"muse-vault/v1/index\0".to_vec();
    lp16(&mut message, kind.as_bytes());
    lp16(&mut message, field.as_bytes());
    lp16(&mut message, value.as_bytes());
    let tag = hmac256(k_index.expose(), &[&message]);
    message.zeroize();
    hex(&tag[..16])
}

/// The tag the store files a record under instead of its kind:
/// the first 16 bytes, in hex, of `HMAC-SHA-256(K_index, "muse-vault/v1/kind\0" || kind)`.
pub fn kind_tag(k_index: &Secret32, kind: &str) -> String {
    hex(&hmac256(k_index.expose(), &[b"muse-vault/v1/kind\0", kind.as_bytes()])[..16])
}

/// A keyed reference: `HMAC-SHA-256(K_ref, lp16(label) || data)`. Records and
/// receipts use it to name a thing without revealing or allowing a guess at it.
pub fn keyed_ref(k_ref: &Secret32, label: &str, data: &[u8]) -> [u8; 32] {
    let mut prefix = Vec::new();
    lp16(&mut prefix, label.as_bytes());
    hmac256(k_ref.expose(), &[&prefix, data])
}

// ---------------------------------------------------------------- recovery words check

/// The 4-bit check values for the 24 words of a recovery key, packed two per
/// byte (word 0 in the high half of byte 0). Value `i` is the low 4 bits of
/// the first byte of
/// `SHA-256("muse-vault/v1/word-check\0" || salt || u8(i) || u16_be(word_index))`.
pub fn word_check_table(salt: &[u8; 16], indices: &[u16; 24]) -> Zeroizing<[u8; 12]> {
    let mut out = Zeroizing::new([0u8; 12]);
    for (i, index) in indices.iter().enumerate() {
        let nibble = word_check_nibble(salt, i as u8, *index);
        out[i / 2] |= if i % 2 == 0 { nibble << 4 } else { nibble };
    }
    out
}

/// One check value; see [`word_check_table`].
pub fn word_check_nibble(salt: &[u8; 16], position: u8, index: u16) -> u8 {
    let mut hasher = Sha256::new();
    hasher.update(b"muse-vault/v1/word-check\0");
    hasher.update(salt);
    hasher.update([position]);
    hasher.update(index.to_be_bytes());
    hasher.finalize()[0] & 0x0f
}

// ---------------------------------------------------------------- passphrase

/// Argon2id (version 0x13) to 32 bytes. `None` if the parameters are outside
/// what the algorithm accepts.
pub fn argon2id(passphrase: &[u8], salt: &[u8], m_kib: u32, t: u32, p: u32) -> Option<Secret32> {
    let params = Params::new(m_kib, t, p, Some(32)).ok()?;
    let mut out = [0u8; 32];
    Argon2::new(Algorithm::Argon2id, Version::V0x13, params).hash_password_into(passphrase, salt, &mut out).ok()?;
    let key = Secret32::new(out);
    out.zeroize();
    Some(key)
}

/// The 32 bytes behind the age identity that opens exports:
/// `HKDF-SHA-256(salt = "", ikm = recovery entropy, info = "muse-vault/v1/export-x25519")`.
pub fn export_identity_bytes(recovery_entropy: &Secret32) -> Secret32 {
    hkdf32(b"", recovery_entropy.expose(), b"muse-vault/v1/export-x25519")
}
