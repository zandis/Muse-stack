//! Where a device keeps its own secret: the `DeviceKeyStore` trait, an
//! in-memory store for tests, and a passphrase-protected store for the
//! headless home.
//!
//! The vault never gives a keystore the master key. It gives it 32 random
//! bytes (the device's slot secret) and keeps the returned blob in the header.
//! Platform stores (Keychain / Secure Enclave, Android Keystore, DPAPI / TPM,
//! Secret Service) come with the app shell.

use std::fmt;

use serde::{Deserialize, Serialize};
use unicode_normalization::UnicodeNormalization;
use zeroize::Zeroizing;

use super::primitives::{argon2id, hkdf32, hmac256, open, seal, NONCE_LEN, TAG_LEN};
use super::secret::{hex, unhex, unhex_array, Secret32, VaultRng};

/// A keystore failed or refused. Carries no detail on purpose.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyStoreError;

impl fmt::Display for KeyStoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the device keystore could not complete the operation")
    }
}

impl std::error::Error for KeyStoreError {}

/// A device's own key, held where the platform keeps keys.
///
/// `wrap` returns an opaque blob that only this keystore can turn back into
/// `secret`; `unwrap` must fail (not return garbage) for a blob it did not
/// make or that was altered. An implementation may ask for biometrics or a PIN
/// inside `unwrap`.
pub trait DeviceKeyStore {
    fn wrap(&self, secret: &[u8]) -> Result<Vec<u8>, KeyStoreError>;
    fn unwrap(&self, blob: &[u8]) -> Result<Zeroizing<Vec<u8>>, KeyStoreError>;
}

/// Deterministic authenticated wrap under a 32-byte key:
/// `nonce = HMAC-SHA-256(K_nonce, secret)[..24]`, `blob = nonce || XChaCha20-Poly1305(K_wrap, nonce, aad, secret)`
/// with `K_nonce`, `K_wrap` = HKDF-SHA-256 of the key (info `muse-vault/v1/keystore/nonce`, `.../wrap`).
/// A nonce derived from the message means no random source is needed and the
/// same secret always gives the same blob, which is harmless for a key wrap.
fn wrap_with(key: &Secret32, secret: &[u8]) -> Vec<u8> {
    let k_nonce = hkdf32(b"", key.expose(), b"muse-vault/v1/keystore/nonce");
    let k_wrap = hkdf32(b"", key.expose(), b"muse-vault/v1/keystore/wrap");
    let mut nonce = [0u8; NONCE_LEN];
    nonce.copy_from_slice(&hmac256(k_nonce.expose(), &[secret])[..NONCE_LEN]);
    let mut blob = nonce.to_vec();
    blob.extend_from_slice(&seal(&k_wrap, &nonce, b"muse-vault/v1/keystore", secret));
    blob
}

fn unwrap_with(key: &Secret32, blob: &[u8]) -> Result<Zeroizing<Vec<u8>>, KeyStoreError> {
    if blob.len() < NONCE_LEN + TAG_LEN {
        return Err(KeyStoreError);
    }
    let k_wrap = hkdf32(b"", key.expose(), b"muse-vault/v1/keystore/wrap");
    let nonce: [u8; NONCE_LEN] = blob[..NONCE_LEN].try_into().map_err(|_| KeyStoreError)?;
    open(&k_wrap, &nonce, b"muse-vault/v1/keystore", &blob[NONCE_LEN..]).ok_or(KeyStoreError)
}

/// A keystore that is just a key in memory. For tests, and for a shell that
/// fetches one 32-byte key from the platform store and wants the core to do
/// the wrapping.
#[derive(Clone)]
pub struct MemoryKeyStore {
    key: Secret32,
}

impl MemoryKeyStore {
    pub fn new(rng: &mut dyn VaultRng) -> Result<Self, super::VaultError> {
        Ok(MemoryKeyStore { key: Secret32::random(rng)? })
    }

    pub fn from_key(key: Secret32) -> Self {
        MemoryKeyStore { key }
    }
}

impl fmt::Debug for MemoryKeyStore {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("MemoryKeyStore(<redacted>)")
    }
}

impl DeviceKeyStore for MemoryKeyStore {
    fn wrap(&self, secret: &[u8]) -> Result<Vec<u8>, KeyStoreError> {
        Ok(wrap_with(&self.key, secret))
    }

    fn unwrap(&self, blob: &[u8]) -> Result<Zeroizing<Vec<u8>>, KeyStoreError> {
        unwrap_with(&self.key, blob)
    }
}

/// Argon2id cost for [`PassphraseKeyStore`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Argon2Params {
    /// Memory in KiB.
    pub m_kib: u32,
    /// Passes.
    pub t: u32,
    /// Lanes.
    pub p: u32,
}

impl Argon2Params {
    /// 64 MiB, 3 passes, 1 lane: the second recommended setting of RFC 9106
    /// (section 4, "memory-constrained"), with one lane because this build
    /// computes lanes one after another and the smallest home is a one-core VM.
    /// Fits a 1 GB VM and any phone; about a second on a small VM.
    pub const DEFAULT: Argon2Params = Argon2Params { m_kib: 64 * 1024, t: 3, p: 1 };
    /// The weakest setting a key file may carry: 19 MiB, 2 passes (the OWASP
    /// minimum for Argon2id). A file asking for less is refused, so nobody can
    /// make the home accept a cheap-to-guess key file.
    pub const MINIMUM: Argon2Params = Argon2Params { m_kib: 19 * 1024, t: 2, p: 1 };
    /// The most a key file may ask for (1 GiB, 16 passes, 4 lanes), so a
    /// planted file cannot exhaust the machine.
    pub const MAXIMUM: Argon2Params = Argon2Params { m_kib: 1024 * 1024, t: 16, p: 4 };

    pub fn acceptable(self) -> bool {
        (Self::MINIMUM.m_kib..=Self::MAXIMUM.m_kib).contains(&self.m_kib)
            && (Self::MINIMUM.t..=Self::MAXIMUM.t).contains(&self.t)
            && (Self::MINIMUM.p..=Self::MAXIMUM.p).contains(&self.p)
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct KeyFile {
    format: String,
    version: u32,
    kdf: String,
    m_kib: u32,
    t: u32,
    p: u32,
    salt: String,
    nonce: String,
    wrapped: String,
}

const KEYFILE_FORMAT: &str = "muse-vault-keyfile";
const KEYFILE_VERSION: u32 = 1;

fn keyfile_aad(params: Argon2Params, salt: &[u8; 16]) -> Vec<u8> {
    let mut aad = b"muse-vault/v1/keyfile".to_vec();
    aad.extend_from_slice(&KEYFILE_VERSION.to_be_bytes());
    aad.extend_from_slice(&params.m_kib.to_be_bytes());
    aad.extend_from_slice(&params.t.to_be_bytes());
    aad.extend_from_slice(&params.p.to_be_bytes());
    aad.extend_from_slice(salt);
    aad
}

/// NFKC, then UTF-8: the same passphrase typed with a full-width or a
/// half-width keyboard, composed or decomposed, gives the same key.
fn passphrase_bytes(passphrase: &str) -> Zeroizing<Vec<u8>> {
    let normalised: Zeroizing<String> = Zeroizing::new(passphrase.nfkc().collect());
    Zeroizing::new(normalised.as_bytes().to_vec())
}

/// The keystore of a machine with no platform keystore to speak of (the
/// always-on home, a VM): a random device key, wrapped under a key stretched
/// from a passphrase with Argon2id, kept in a small key file.
///
/// The core does not open files. `create` returns the key file's bytes for the
/// caller to write (mode 600); `open` takes them back.
pub struct PassphraseKeyStore {
    device_key: Secret32,
}

impl fmt::Debug for PassphraseKeyStore {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("PassphraseKeyStore(<redacted>)")
    }
}

impl PassphraseKeyStore {
    /// A new device key under `passphrase`. Returns the store and the key file.
    pub fn create(
        passphrase: &str,
        params: Argon2Params,
        rng: &mut dyn VaultRng,
    ) -> Result<(Self, Vec<u8>), KeyStoreError> {
        let device_key = Secret32::random(rng).map_err(|_| KeyStoreError)?;
        let store = PassphraseKeyStore { device_key };
        let file = store.key_file(passphrase, params, rng)?;
        Ok((store, file))
    }

    /// The key file for this device key under a (new) passphrase.
    pub fn key_file(
        &self,
        passphrase: &str,
        params: Argon2Params,
        rng: &mut dyn VaultRng,
    ) -> Result<Vec<u8>, KeyStoreError> {
        if passphrase.is_empty() || !params.acceptable() {
            return Err(KeyStoreError);
        }
        let mut salt = [0u8; 16];
        let mut nonce = [0u8; NONCE_LEN];
        rng.fill(&mut salt).map_err(|_| KeyStoreError)?;
        rng.fill(&mut nonce).map_err(|_| KeyStoreError)?;
        let kek = argon2id(&passphrase_bytes(passphrase), &salt, params.m_kib, params.t, params.p).ok_or(KeyStoreError)?;
        let wrapped = seal(&kek, &nonce, &keyfile_aad(params, &salt), self.device_key.expose());
        let file = KeyFile {
            format: KEYFILE_FORMAT.to_string(),
            version: KEYFILE_VERSION,
            kdf: "argon2id".to_string(),
            m_kib: params.m_kib,
            t: params.t,
            p: params.p,
            salt: hex(&salt),
            nonce: hex(&nonce),
            wrapped: hex(&wrapped),
        };
        serde_json::to_vec(&file).map_err(|_| KeyStoreError)
    }

    /// Opens a key file. A wrong passphrase, an altered file and out-of-range
    /// parameters all give the same error.
    pub fn open(passphrase: &str, key_file: &[u8]) -> Result<Self, KeyStoreError> {
        let file: KeyFile = serde_json::from_slice(key_file).map_err(|_| KeyStoreError)?;
        let params = Argon2Params { m_kib: file.m_kib, t: file.t, p: file.p };
        if file.format != KEYFILE_FORMAT
            || file.version != KEYFILE_VERSION
            || file.kdf != "argon2id"
            || !params.acceptable()
            || passphrase.is_empty()
        {
            return Err(KeyStoreError);
        }
        let salt: [u8; 16] = unhex_array(&file.salt).ok_or(KeyStoreError)?;
        let nonce: [u8; NONCE_LEN] = unhex_array(&file.nonce).ok_or(KeyStoreError)?;
        let wrapped = unhex(&file.wrapped).ok_or(KeyStoreError)?;
        let kek = argon2id(&passphrase_bytes(passphrase), &salt, params.m_kib, params.t, params.p).ok_or(KeyStoreError)?;
        let plain = open(&kek, &nonce, &keyfile_aad(params, &salt), &wrapped).ok_or(KeyStoreError)?;
        let device_key = Secret32::from_slice(&plain).ok_or(KeyStoreError)?;
        Ok(PassphraseKeyStore { device_key })
    }
}

impl DeviceKeyStore for PassphraseKeyStore {
    fn wrap(&self, secret: &[u8]) -> Result<Vec<u8>, KeyStoreError> {
        Ok(wrap_with(&self.device_key, secret))
    }

    fn unwrap(&self, blob: &[u8]) -> Result<Zeroizing<Vec<u8>>, KeyStoreError> {
        unwrap_with(&self.device_key, blob)
    }
}
