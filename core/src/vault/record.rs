//! One record on disk: a small *head* (the wrapped record key, the kind and
//! the indexed fields) and a *body* (the padded content under the record key).

use zeroize::{Zeroize, Zeroizing};

use super::primitives::{head_bucket, open, pad_payload, seal, unpad_payload, NONCE_LEN, TAG_LEN};
use super::secret::Secret32;

pub const HEAD_MAGIC: &[u8; 4] = b"MVH1";
pub const BODY_MAGIC: &[u8; 4] = b"MVB1";
pub const STATE_LIVE: u8 = 0;
pub const STATE_TOMBSTONE: u8 = 1;
pub const MAX_FIELDS: usize = 16;
pub const MAX_FIELD_VALUE: usize = 1024;
const HEAD_FIXED: usize = 4 + 1 + 8 + 4 + 8 + NONCE_LEN;

/// The clear part of a head. All of it is bound into the head's associated data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HeadMeta {
    /// [`STATE_LIVE`] or [`STATE_TOMBSTONE`].
    pub state: u8,
    /// 1 for a new record, +1 for each update or the deletion.
    pub version: u64,
    /// The caller's schema number for the content.
    pub schema: u32,
    /// The master-key epoch whose record-wrap key sealed this head.
    pub epoch: u64,
}

/// What a live head holds once opened.
pub struct HeadPlain {
    pub data_key: Secret32,
    pub kind: String,
    /// (field name, normalised value) pairs that are blind-indexed.
    pub fields: Vec<(String, String)>,
}

impl Drop for HeadPlain {
    fn drop(&mut self) {
        self.kind.zeroize();
        for (name, value) in self.fields.iter_mut() {
            name.zeroize();
            value.zeroize();
        }
    }
}

/// A kind or field name: 1 to 64 of `a-z 0-9 . _ -`.
pub fn valid_name(name: &str) -> bool {
    (1..=64).contains(&name.len())
        && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b'-'))
}

fn lp16(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u16).to_be_bytes());
    out.extend_from_slice(bytes);
}

struct Reader<'a>(&'a [u8]);

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Option<&'a [u8]> {
        if self.0.len() < n {
            return None;
        }
        let (front, rest) = self.0.split_at(n);
        self.0 = rest;
        Some(front)
    }

    fn u16(&mut self) -> Option<usize> {
        Some(u16::from_be_bytes(self.take(2)?.try_into().ok()?) as usize)
    }

    fn text(&mut self) -> Option<String> {
        let n = self.u16()?;
        String::from_utf8(self.take(n)?.to_vec()).ok()
    }
}

impl HeadPlain {
    /// `u16_be(L) || body || zeros` to a multiple of 64 bytes (at least 128), where
    /// `body = data_key(32) || lp16(kind) || u16_be(n) || n x (lp16(field) || lp16(value))`.
    pub fn encode(&self) -> Zeroizing<Vec<u8>> {
        let mut body = Zeroizing::new(Vec::new());
        body.extend_from_slice(self.data_key.expose());
        lp16(&mut body, self.kind.as_bytes());
        body.extend_from_slice(&(self.fields.len() as u16).to_be_bytes());
        for (name, value) in &self.fields {
            lp16(&mut body, name.as_bytes());
            lp16(&mut body, value.as_bytes());
        }
        let mut out = Zeroizing::new(Vec::with_capacity(head_bucket(body.len() + 2)));
        out.extend_from_slice(&(body.len() as u16).to_be_bytes());
        out.extend_from_slice(&body);
        let target = head_bucket(out.len());
        out.resize(target, 0);
        out
    }

    pub fn decode(padded: &[u8]) -> Option<HeadPlain> {
        let mut outer = Reader(padded);
        let len = outer.u16()?;
        let body = outer.take(len)?;
        if padded.len() != head_bucket(len + 2) || outer.0.iter().any(|&b| b != 0) {
            return None;
        }
        let mut r = Reader(body);
        let data_key = Secret32::from_slice(r.take(32)?)?;
        let kind = r.text()?;
        let count = r.u16()?;
        if !valid_name(&kind) || count > MAX_FIELDS {
            return None;
        }
        let mut fields = Vec::with_capacity(count);
        for _ in 0..count {
            let name = r.text()?;
            let value = r.text()?;
            if !valid_name(&name) || value.len() > MAX_FIELD_VALUE {
                return None;
            }
            fields.push((name, value));
        }
        if !r.0.is_empty() {
            return None;
        }
        Some(HeadPlain { data_key, kind, fields })
    }
}

/// Associated data of a head:
/// `"muse-vault/v1/record-head" || vault_id(16) || record_id(16) || state(1) || version(8) || schema(4) || epoch(8)`.
pub fn head_aad(vault_id: &[u8; 16], record_id: &[u8; 16], meta: HeadMeta) -> Vec<u8> {
    let mut aad = b"muse-vault/v1/record-head".to_vec();
    aad.extend_from_slice(vault_id);
    aad.extend_from_slice(record_id);
    aad.push(meta.state);
    aad.extend_from_slice(&meta.version.to_be_bytes());
    aad.extend_from_slice(&meta.schema.to_be_bytes());
    aad.extend_from_slice(&meta.epoch.to_be_bytes());
    aad
}

/// Associated data of a body:
/// `"muse-vault/v1/record-body" || vault_id(16) || record_id(16) || version(8) || schema(4) || lp16(kind)`.
/// No epoch: a rotation re-wraps heads and leaves bodies alone.
pub fn body_aad(vault_id: &[u8; 16], record_id: &[u8; 16], version: u64, schema: u32, kind: &str) -> Vec<u8> {
    let mut aad = b"muse-vault/v1/record-body".to_vec();
    aad.extend_from_slice(vault_id);
    aad.extend_from_slice(record_id);
    aad.extend_from_slice(&version.to_be_bytes());
    aad.extend_from_slice(&schema.to_be_bytes());
    lp16(&mut aad, kind.as_bytes());
    aad
}

/// `"MVH1" || state || version || schema || epoch || nonce(24) || box`.
/// For a live record the box holds [`HeadPlain::encode`]; for a tombstone it
/// is the 16-byte tag over nothing, which still proves the deletion was made
/// by someone holding the key.
pub fn seal_head(
    k_record: &Secret32,
    vault_id: &[u8; 16],
    record_id: &[u8; 16],
    meta: HeadMeta,
    nonce: &[u8; NONCE_LEN],
    plain: Option<&HeadPlain>,
) -> Vec<u8> {
    let aad = head_aad(vault_id, record_id, meta);
    let boxed = match plain {
        Some(plain) => seal(k_record, nonce, &aad, &plain.encode()),
        None => seal(k_record, nonce, &aad, b""),
    };
    let mut out = Vec::with_capacity(HEAD_FIXED + boxed.len());
    out.extend_from_slice(HEAD_MAGIC);
    out.push(meta.state);
    out.extend_from_slice(&meta.version.to_be_bytes());
    out.extend_from_slice(&meta.schema.to_be_bytes());
    out.extend_from_slice(&meta.epoch.to_be_bytes());
    out.extend_from_slice(nonce);
    out.extend_from_slice(&boxed);
    out
}

/// The clear part of a head, without opening it. `None` if it is not a head.
pub fn head_meta(head: &[u8]) -> Option<HeadMeta> {
    if head.len() < HEAD_FIXED + TAG_LEN || &head[..4] != HEAD_MAGIC {
        return None;
    }
    let state = head[4];
    if state != STATE_LIVE && state != STATE_TOMBSTONE {
        return None;
    }
    Some(HeadMeta {
        state,
        version: u64::from_be_bytes(head[5..13].try_into().ok()?),
        schema: u32::from_be_bytes(head[13..17].try_into().ok()?),
        epoch: u64::from_be_bytes(head[17..25].try_into().ok()?),
    })
}

/// Opens a head. `None` for any failure. `Some((meta, None))` is an authentic tombstone.
pub fn open_head(
    k_record: &Secret32,
    vault_id: &[u8; 16],
    record_id: &[u8; 16],
    head: &[u8],
) -> Option<(HeadMeta, Option<HeadPlain>)> {
    let meta = head_meta(head)?;
    let nonce: [u8; NONCE_LEN] = head[25..HEAD_FIXED].try_into().ok()?;
    let plain = open(k_record, &nonce, &head_aad(vault_id, record_id, meta), &head[HEAD_FIXED..])?;
    if meta.state == STATE_TOMBSTONE {
        return plain.is_empty().then_some((meta, None));
    }
    Some((meta, Some(HeadPlain::decode(&plain)?)))
}

/// `"MVB1" || nonce(24) || XChaCha20-Poly1305(data_key, nonce, body_aad, pad(payload))`.
pub fn seal_body(
    data_key: &Secret32,
    vault_id: &[u8; 16],
    record_id: &[u8; 16],
    meta: HeadMeta,
    kind: &str,
    nonce: &[u8; NONCE_LEN],
    payload: &[u8],
) -> Vec<u8> {
    let aad = body_aad(vault_id, record_id, meta.version, meta.schema, kind);
    let boxed = seal(data_key, nonce, &aad, &pad_payload(payload));
    let mut out = Vec::with_capacity(4 + NONCE_LEN + boxed.len());
    out.extend_from_slice(BODY_MAGIC);
    out.extend_from_slice(nonce);
    out.extend_from_slice(&boxed);
    out
}

pub fn open_body(
    data_key: &Secret32,
    vault_id: &[u8; 16],
    record_id: &[u8; 16],
    meta: HeadMeta,
    kind: &str,
    body: &[u8],
) -> Option<Zeroizing<Vec<u8>>> {
    if body.len() < 4 + NONCE_LEN + TAG_LEN || &body[..4] != BODY_MAGIC {
        return None;
    }
    let nonce: [u8; NONCE_LEN] = body[4..4 + NONCE_LEN].try_into().ok()?;
    let aad = body_aad(vault_id, record_id, meta.version, meta.schema, kind);
    let padded = open(data_key, &nonce, &aad, &body[4 + NONCE_LEN..])?;
    unpad_payload(&padded)
}
