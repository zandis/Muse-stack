//! The Noise handshake and the encrypted message channel that follows it.
//!
//! Pattern: `Noise_NNpsk0_25519_ChaChaPoly_SHA256` (the `snow` crate). The
//! pre-shared key comes from the invitation's one-time secret; the prologue
//! ties the handshake to this invitation, both link ids and this very TLS
//! connection, so it cannot be carried over to another one.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use iroh::endpoint::{Connection, RecvStream, SendStream, VarInt};
use sha2::{Digest, Sha256};
use snow::params::{CipherChoice, DHChoice, HashChoice, NoiseParams};
use snow::resolvers::{CryptoResolver, DefaultResolver};
use snow::types::{Cipher, Dh, Hash, Random};
use tokio::time::timeout;

use super::messages::{AbortReason, Msg};
use super::PairingError;
use crate::endpoint::{CLOSE_NORMAL, CLOSE_PAIRING, PAIR_ALPN};
use crate::frame::{read_frame, write_frame, Frame, FrameType};

pub(crate) const NOISE_PATTERN: &str = "Noise_NNpsk0_25519_ChaChaPoly_SHA256";
const EXPORTER_LABEL: &[u8] = b"muse/pair/1 channel binding";
const CHUNK: usize = 60_000;
/// The largest message after reassembly.
const MAX_MESSAGE: usize = 1024 * 1024;

/// Every decrypted or clear byte string a session sent or received, for tests
/// that prove no key is among them.
pub type PlaintextLog = Arc<Mutex<Vec<Vec<u8>>>>;

pub(crate) fn log_plaintext(log: &Option<PlaintextLog>, bytes: &[u8]) {
    if let Some(log) = log {
        if let Ok(mut log) = log.lock() {
            log.push(bytes.to_vec());
        }
    }
}

/// A deterministic stream from a 32-byte seed: block `i` is `SHA-256(seed || u64_be(i))`.
/// The seed comes from the injected random source (the OS in a product build),
/// so tests are repeatable and there is one code path.
struct SeededRandom {
    seed: [u8; 32],
    counter: u64,
}

impl Random for SeededRandom {
    fn try_fill_bytes(&mut self, dest: &mut [u8]) -> Result<(), snow::Error> {
        for chunk in dest.chunks_mut(32) {
            let mut h = Sha256::new();
            h.update(self.seed);
            h.update(self.counter.to_be_bytes());
            self.counter += 1;
            chunk.copy_from_slice(&h.finalize()[..chunk.len()]);
        }
        Ok(())
    }
}

impl Drop for SeededRandom {
    fn drop(&mut self) {
        self.seed = [0; 32];
    }
}

struct SeededResolver {
    seed: [u8; 32],
}

impl CryptoResolver for SeededResolver {
    fn resolve_rng(&self) -> Option<Box<dyn Random>> {
        Some(Box::new(SeededRandom { seed: self.seed, counter: 0 }))
    }

    fn resolve_dh(&self, choice: &DHChoice) -> Option<Box<dyn Dh>> {
        DefaultResolver.resolve_dh(choice)
    }

    fn resolve_hash(&self, choice: &HashChoice) -> Option<Box<dyn Hash>> {
        DefaultResolver.resolve_hash(choice)
    }

    fn resolve_cipher(&self, choice: &CipherChoice) -> Option<Box<dyn Cipher>> {
        DefaultResolver.resolve_cipher(choice)
    }
}

/// What both sides must agree on before the handshake means anything.
pub(crate) fn prologue(conn: &Connection, invite_id: &[u8; 8], expires: u64, inviter: &[u8; 32], joiner: &[u8; 32]) -> Result<Vec<u8>, PairingError> {
    let mut exporter = [0u8; 32];
    conn.export_keying_material(&mut exporter, EXPORTER_LABEL, invite_id).map_err(|_| PairingError::Protocol("no channel binding"))?;
    let mut out = Vec::with_capacity(160);
    out.extend_from_slice(PAIR_ALPN);
    out.push(0);
    out.push(crate::endpoint::PAIR_VERSION);
    out.extend_from_slice(NOISE_PATTERN.as_bytes());
    out.push(0);
    out.extend_from_slice(invite_id);
    out.extend_from_slice(&expires.to_be_bytes());
    out.extend_from_slice(inviter);
    out.extend_from_slice(joiner);
    out.extend_from_slice(&exporter);
    Ok(out)
}

pub(crate) fn handshake(initiator: bool, psk: &[u8; 32], prologue: &[u8], seed: [u8; 32]) -> Result<snow::HandshakeState, PairingError> {
    let params: NoiseParams = NOISE_PATTERN.parse().map_err(|_| PairingError::Protocol("noise parameters"))?;
    let builder = snow::Builder::with_resolver(params, Box::new(SeededResolver { seed }))
        .psk(0, psk)
        .and_then(|b| b.prologue(prologue))
        .map_err(|_| PairingError::Protocol("noise setup"))?;
    let state = if initiator { builder.build_initiator() } else { builder.build_responder() };
    state.map_err(|_| PairingError::Protocol("noise setup"))
}

/// The streams of one pairing connection, before and after the handshake.
pub(crate) struct Wire {
    pub conn: Connection,
    pub send: SendStream,
    pub recv: RecvStream,
    pub log: Option<PlaintextLog>,
}

impl Wire {
    pub async fn write(&mut self, kind: FrameType, payload: &[u8]) -> Result<(), PairingError> {
        write_frame(&mut self.send, kind, payload).await.map_err(|e| PairingError::Closed(e.to_string()))
    }

    pub async fn read(&mut self, wait: Duration) -> Result<Frame, PairingError> {
        match timeout(wait, read_frame(&mut self.recv)).await {
            Err(_) => Err(PairingError::Timeout),
            Ok(Err(e)) => Err(PairingError::Closed(e.to_string())),
            Ok(Ok(frame)) => Ok(frame),
        }
    }

    /// Says why in clear (only used before the session keys exist) and closes.
    pub async fn abort_clear(&mut self, reason: AbortReason) {
        let _ = write_frame(&mut self.send, FrameType::PairAbort, &[reason as u8]).await;
        self.close_after_flush().await;
    }

    /// Lets the last frame reach the other side, then closes the connection.
    pub async fn close_after_flush(&mut self) {
        let _ = self.send.finish();
        let _ = timeout(Duration::from_secs(2), self.conn.closed()).await;
        self.conn.close(VarInt::from_u32(CLOSE_PAIRING), b"pairing ended");
    }

    pub fn close(&self) {
        self.conn.close(VarInt::from_u32(CLOSE_NORMAL), b"done");
    }
}

/// Encrypts and decrypts session messages, with no I/O. A message is cut into
/// pieces of at most 60 000 bytes; each piece starts with one byte saying
/// whether more follow. A changed, dropped, repeated or reordered piece fails
/// to decrypt (Noise counts messages), and the session must end.
pub(crate) struct Sealer {
    transport: snow::TransportState,
    partial: Vec<u8>,
}

impl Sealer {
    pub fn new(transport: snow::TransportState) -> Sealer {
        Sealer { transport, partial: Vec::new() }
    }

    /// The ciphertext pieces for one message, in order.
    pub fn seal(&mut self, plain: &[u8]) -> Result<Vec<Vec<u8>>, PairingError> {
        if plain.is_empty() || plain.len() > MAX_MESSAGE {
            return Err(PairingError::Protocol("message size"));
        }
        let mut out = Vec::new();
        let mut buf = vec![0u8; CHUNK + 64];
        let mut chunks = plain.chunks(CHUNK).peekable();
        while let Some(chunk) = chunks.next() {
            let mut piece = Vec::with_capacity(chunk.len() + 1);
            piece.push(u8::from(chunks.peek().is_some()));
            piece.extend_from_slice(chunk);
            let n = self.transport.write_message(&piece, &mut buf).map_err(|_| PairingError::Protocol("encrypt"))?;
            out.push(buf[..n].to_vec());
        }
        Ok(out)
    }

    /// Takes the next piece; `Some(message)` when it completes one.
    pub fn open(&mut self, piece: &[u8]) -> Result<Option<Vec<u8>>, PairingError> {
        let mut buf = vec![0u8; 65535];
        let n = self.transport.read_message(piece, &mut buf).map_err(|_| PairingError::Tampered)?;
        if n < 2 || self.partial.len() + n - 1 > MAX_MESSAGE {
            return Err(PairingError::Protocol("message size"));
        }
        self.partial.extend_from_slice(&buf[1..n]);
        match buf[0] {
            0 => Ok(Some(std::mem::take(&mut self.partial))),
            1 => Ok(None),
            _ => Err(PairingError::Protocol("chunk marker")),
        }
    }
}

/// The session after the handshake: typed messages, encrypted and in order.
pub(crate) struct SecureChannel {
    pub wire: Wire,
    sealer: Sealer,
}

impl SecureChannel {
    pub fn new(wire: Wire, transport: snow::TransportState) -> SecureChannel {
        SecureChannel { wire, sealer: Sealer::new(transport) }
    }

    pub async fn send(&mut self, msg: &Msg) -> Result<(), PairingError> {
        let plain = msg.encode();
        log_plaintext(&self.wire.log, &plain);
        for piece in self.sealer.seal(&plain)? {
            self.wire.write(FrameType::PairData, &piece).await?;
        }
        Ok(())
    }

    pub async fn recv(&mut self, wait: Duration) -> Result<Msg, PairingError> {
        let deadline = tokio::time::Instant::now() + wait;
        let plain = loop {
            let left = deadline.saturating_duration_since(tokio::time::Instant::now());
            let frame = self.wire.read(left).await?;
            if frame.kind != FrameType::PairData {
                return Err(PairingError::Protocol("not a session frame"));
            }
            if let Some(plain) = self.sealer.open(&frame.payload)? {
                break plain;
            }
        };
        log_plaintext(&self.wire.log, &plain);
        Msg::decode(&plain).map_err(|_| PairingError::Protocol("malformed message"))
    }

    /// Tells the other side why, inside the session, and closes.
    pub async fn abort(&mut self, reason: AbortReason) {
        let _ = self.send(&Msg::Abort(reason)).await;
        self.wire.close_after_flush().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(psk_a: &[u8; 32], psk_b: &[u8; 32], pro_a: &[u8], pro_b: &[u8]) -> Result<(Vec<u8>, Vec<u8>), ()> {
        let mut i = handshake(true, psk_a, pro_a, [1; 32]).unwrap();
        let mut r = handshake(false, psk_b, pro_b, [2; 32]).unwrap();
        let (mut m, mut p) = (vec![0u8; 1024], vec![0u8; 1024]);
        let n = i.write_message(&[], &mut m).unwrap();
        r.read_message(&m[..n], &mut p).map_err(|_| ())?;
        let n = r.write_message(b"commit", &mut m).unwrap();
        i.read_message(&m[..n], &mut p).map_err(|_| ())?;
        Ok((i.get_handshake_hash().to_vec(), r.get_handshake_hash().to_vec()))
    }

    #[test]
    fn handshake_needs_the_same_secret_and_the_same_prologue() {
        let (a, b) = pair(&[5; 32], &[5; 32], b"same", b"same").unwrap();
        assert_eq!(a, b);
        assert!(pair(&[5; 32], &[6; 32], b"same", b"same").is_err());
        assert!(pair(&[5; 32], &[5; 32], b"one connection", b"another connection").is_err());
    }

    #[test]
    fn the_seed_makes_the_handshake_repeatable_and_distinct_seeds_differ() {
        let first = |seed: [u8; 32]| {
            let mut i = handshake(true, &[5; 32], b"p", seed).unwrap();
            let mut m = vec![0u8; 256];
            let n = i.write_message(&[], &mut m).unwrap();
            m[..n].to_vec()
        };
        assert_eq!(first([1; 32]), first([1; 32]));
        assert_ne!(first([1; 32]), first([2; 32]));
    }

    fn sealers() -> (Sealer, Sealer) {
        let mut i = handshake(true, &[5; 32], b"p", [1; 32]).unwrap();
        let mut r = handshake(false, &[5; 32], b"p", [2; 32]).unwrap();
        let (mut m, mut p) = (vec![0u8; 1024], vec![0u8; 1024]);
        let n = i.write_message(&[], &mut m).unwrap();
        r.read_message(&m[..n], &mut p).unwrap();
        let n = r.write_message(&[], &mut m).unwrap();
        i.read_message(&m[..n], &mut p).unwrap();
        (Sealer::new(i.into_transport_mode().unwrap()), Sealer::new(r.into_transport_mode().unwrap()))
    }

    #[test]
    fn messages_of_every_size_round_trip_in_pieces() {
        let (mut a, mut b) = sealers();
        for len in [1usize, 2, 59_999, 60_000, 60_001, 120_000, 300_000, MAX_MESSAGE] {
            let plain: Vec<u8> = (0..len).map(|i| (i % 253) as u8).collect();
            let pieces = a.seal(&plain).unwrap();
            assert_eq!(pieces.len(), len.div_ceil(CHUNK));
            assert!(pieces.iter().all(|p| p.len() <= crate::frame::MAX_NOISE_MESSAGE));
            let mut got = None;
            for piece in &pieces {
                assert!(got.is_none());
                got = b.open(piece).unwrap();
            }
            assert!(got.unwrap() == plain);
        }
        assert!(a.seal(&[]).is_err());
        assert!(a.seal(&vec![0u8; MAX_MESSAGE + 1]).is_err());
        // Both directions are independent.
        let back = b.seal(b"reply").unwrap();
        assert_eq!(a.open(&back[0]).unwrap().unwrap(), b"reply");
    }

    #[test]
    fn every_single_bit_flip_in_a_piece_is_caught() {
        let plain = Msg::Confirm.encode();
        let (mut a, _) = sealers();
        let piece = a.seal(&plain).unwrap().remove(0);
        for at in 0..piece.len() {
            for bit in 0..8 {
                let (mut a, mut b) = sealers();
                let mut bad = a.seal(&plain).unwrap().remove(0);
                bad[at] ^= 1 << bit;
                assert_eq!(b.open(&bad), Err(PairingError::Tampered), "byte {at} bit {bit}");
            }
        }
        // Cut short, or lengthened.
        for cut in 0..piece.len() {
            let (mut a, mut b) = sealers();
            let whole = a.seal(&plain).unwrap().remove(0);
            assert!(b.open(&whole[..cut]).is_err(), "cut at {cut}");
        }
        let (mut a, mut b) = sealers();
        let mut longer = a.seal(&plain).unwrap().remove(0);
        longer.push(0);
        assert_eq!(b.open(&longer), Err(PairingError::Tampered));
    }

    #[test]
    fn reordered_repeated_dropped_and_reflected_pieces_are_caught() {
        let msgs: Vec<Vec<u8>> = (0..6u8).map(|i| vec![i + 1; 40]).collect();
        // Every order of delivery other than the right one fails at the first piece out of place.
        let mut state = 0x0123_4567_89ab_cdefu64;
        for _ in 0..200 {
            let (mut a, mut b) = sealers();
            let sealed: Vec<Vec<u8>> = msgs.iter().map(|m| a.seal(m).unwrap().remove(0)).collect();
            let mut order: Vec<usize> = (0..sealed.len()).collect();
            for i in (1..order.len()).rev() {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                order.swap(i, (state >> 33) as usize % (i + 1));
            }
            for (position, index) in order.iter().enumerate() {
                let result = b.open(&sealed[*index]);
                if *index == position {
                    assert_eq!(result.unwrap().unwrap(), msgs[*index]);
                } else {
                    assert_eq!(result, Err(PairingError::Tampered));
                    break;
                }
            }
        }
        // Repeated.
        let (mut a, mut b) = sealers();
        let first = a.seal(&msgs[0]).unwrap().remove(0);
        assert!(b.open(&first).unwrap().is_some());
        assert_eq!(b.open(&first), Err(PairingError::Tampered));
        // Dropped.
        let (mut a, mut b) = sealers();
        let _lost = a.seal(&msgs[0]).unwrap();
        let second = a.seal(&msgs[1]).unwrap().remove(0);
        assert_eq!(b.open(&second), Err(PairingError::Tampered));
        // Sent back to its own sender.
        let (mut a, _) = sealers();
        let own = a.seal(&msgs[0]).unwrap().remove(0);
        assert_eq!(a.open(&own), Err(PairingError::Tampered));
        // A piece of a long message dropped.
        let (mut a, mut b) = sealers();
        let pieces = a.seal(&vec![9u8; 150_000]).unwrap();
        assert_eq!(b.open(&pieces[0]).unwrap(), None);
        assert_eq!(b.open(&pieces[2]), Err(PairingError::Tampered));
        // From another session entirely.
        let (mut a, _) = sealers();
        let mut other = handshake(false, &[5; 32], b"p", [3; 32]).unwrap();
        let mut i = handshake(true, &[5; 32], b"p", [4; 32]).unwrap();
        let (mut m, mut p) = (vec![0u8; 1024], vec![0u8; 1024]);
        let n = i.write_message(&[], &mut m).unwrap();
        other.read_message(&m[..n], &mut p).unwrap();
        let n = other.write_message(&[], &mut m).unwrap();
        i.read_message(&m[..n], &mut p).unwrap();
        let mut stranger = Sealer::new(other.into_transport_mode().unwrap());
        let foreign = a.seal(&msgs[0]).unwrap().remove(0);
        assert_eq!(stranger.open(&foreign), Err(PairingError::Tampered));
    }
}
