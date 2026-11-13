//! Pairing: letting a new device into the vault.
//!
//! The design, its threat table and the reasons are in
//! docs/spec/interfaces/link-and-pairing.md (sections 6 to 9). In short:
//!
//! 1. The inviting device shows an [`Invitation`] (QR code): its link address,
//!    a one-time 128-bit secret, an expiry.
//! 2. The joining device dials that link id on the pairing protocol and names
//!    the invitation. The invitation is taken out of use at that moment.
//! 3. Both run `Noise_NNpsk0` keyed by the secret, with a prologue that ties
//!    the handshake to both link ids and to this TLS connection.
//! 4. Both derive four words from the handshake and two fresh values (one
//!    committed to before the other is seen). The person compares the words
//!    and confirms on both devices.
//! 5. The joining device sends public keys only; the inviting device adds a
//!    vault slot for it and sends back the vault header and the device list.
//!    No vault key crosses the channel in either direction.
//!
//! **This protocol has not had an outside cryptographic review.** It must
//! have one before real use.

mod channel;
mod invitation;
mod messages;

use std::time::Duration;

use muse_core::records::{self, from_hex, to_hex, Actor, Entry, Links, Outcome, RecordStore, RefKey, Signer};
use muse_core::vault::primitives::hkdf32;
use muse_core::vault::{DeviceKeyStore, EnrollRequest, Header, Language, MemoryStore, RecoveryPolicy, Rotation, Vault, VaultRng, VaultStore};
use sha2::{Digest, Sha256};
use tokio::sync::oneshot;
use tokio::time::timeout;
use zeroize::Zeroizing;

pub use channel::PlaintextLog;
pub use invitation::{
    base64url_decode, base64url_encode, qr_version_m, Invitation, InvitationError, INVITATION_VERSION, MAX_DIRECT_ADDRS, MAX_QR_TEXT,
    MAX_RELAY_URL, SECRET_LEN, URL_PREFIX,
};
pub use messages::{AbortReason, Bootstrap, Enroll, Msg};

use self::channel::{handshake, log_plaintext, prologue, SecureChannel, Wire};
use crate::endpoint::{LinkEndpoint, PairingConn, PendingInvite, PAIR_ALPN, PAIR_VERSION};
use crate::error::LinkError;
use crate::frame::FrameType;
use crate::registry::{certify, device_id_of, verify_link_cert, DeviceEntry, DeviceId, DeviceState, RegistryError, Role};

/// How long an invitation may live.
pub const MIN_TTL: Duration = Duration::from_secs(30);
pub const MAX_TTL: Duration = Duration::from_secs(15 * 60);
/// One protocol step between machines.
const STEP_TIMEOUT: Duration = Duration::from_secs(30);
/// How long a side waits for the person on the other device to confirm.
const PERSON_TIMEOUT: Duration = Duration::from_secs(5 * 60);

const COMMIT_DOMAIN: &[u8] = b"muse/pair/1 commit\0";
const SAS_SALT: &[u8] = b"muse/pair/1 sas";
const RNG_SALT: &[u8] = b"muse/pair/1 rng";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PairingError {
    /// The call does not fit the session's state (for example, `finish` before `confirm`).
    State(&'static str),
    /// The invitation's time is up.
    Expired,
    /// The invitation was used or cancelled by an earlier attempt. Show a new code.
    Burned,
    /// The two sides do not share the secret, or are not on the same connection.
    Handshake,
    /// A session message did not decrypt: changed, repeated, dropped or out of order.
    Tampered,
    /// The other side stopped the pairing.
    Aborted(AbortReason),
    /// The other side broke the protocol.
    Protocol(&'static str),
    Timeout,
    /// The connection ended.
    Closed(String),
    Link(LinkError),
    Invitation(InvitationError),
    /// The vault refused (for example, the device name is already used).
    Vault(String),
    Registry(RegistryError),
    /// The new device could not use what it was sent.
    Install(String),
    Rng,
}

impl std::fmt::Display for PairingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PairingError::State(what) => write!(f, "pairing: {what}"),
            PairingError::Expired => f.write_str("the pairing code has expired"),
            PairingError::Burned => f.write_str("the pairing code was already used or cancelled; show a new one"),
            PairingError::Handshake => f.write_str("the devices could not agree; the code does not match this connection"),
            PairingError::Tampered => f.write_str("a pairing message was changed on the way"),
            PairingError::Aborted(reason) => write!(f, "the other device stopped the pairing ({reason:?})"),
            PairingError::Protocol(what) => write!(f, "pairing protocol error: {what}"),
            PairingError::Timeout => f.write_str("the other device did not answer in time"),
            PairingError::Closed(e) => write!(f, "the pairing connection ended: {e}"),
            PairingError::Link(e) => write!(f, "{e}"),
            PairingError::Invitation(e) => write!(f, "{e}"),
            PairingError::Vault(e) => write!(f, "vault: {e}"),
            PairingError::Registry(e) => write!(f, "{e}"),
            PairingError::Install(e) => write!(f, "the new device could not finish: {e}"),
            PairingError::Rng => f.write_str("no random bytes"),
        }
    }
}

impl std::error::Error for PairingError {}

impl From<LinkError> for PairingError {
    fn from(e: LinkError) -> Self {
        PairingError::Link(e)
    }
}

// ---------------------------------------------------------------- records

/// Where pairing and removal are written down. A failure to record never
/// stops or undoes the action (as on the home); the outcome says `recorded: false`.
pub trait RecordSink {
    fn write(&mut self, entry: &Entry) -> Result<(), String>;
}

/// Appends to a device's own chain.
pub struct ChainSink<'a, R: RecordStore> {
    pub store: &'a mut R,
    pub signer: &'a dyn Signer,
    pub ref_key: &'a dyn RefKey,
}

impl<R: RecordStore> RecordSink for ChainSink<'_, R> {
    fn write(&mut self, entry: &Entry) -> Result<(), String> {
        records::write(self.store, entry, self.signer, self.ref_key).map(|_| ()).map_err(|_| "the record could not be written".to_string())
    }
}

/// Writes nothing (and says so: `recorded` is false).
pub struct NoRecords;

impl RecordSink for NoRecords {
    fn write(&mut self, _entry: &Entry) -> Result<(), String> {
        Err("no record store".to_string())
    }
}

/// The action names this crate writes (record-format section 1.3).
pub const ACTION_PAIR: &str = "device.pair";
pub const ACTION_REMOVE: &str = "device.remove";

fn device_entry(now: u64, action: &str, outcome: Outcome, summary: &str, device: &DeviceId) -> Entry {
    Entry {
        time: now.saturating_mul(1000),
        actor: Actor::Person,
        action: action.to_string(),
        outcome,
        summary: summary.to_string(),
        mandate: None,
        resource: Some(format!("device:{}", to_hex(device))),
        cost: None,
        links: Links::default(),
        egress: Some("none".to_string()),
    }
}

// ---------------------------------------------------------------- words

/// The four check words: the first 44 bits of the check value, 11 bits a word.
pub fn check_words(value: &[u8; 32], language: Language) -> [&'static str; 4] {
    let bits = u64::from_be_bytes([value[0], value[1], value[2], value[3], value[4], value[5], value[6], value[7]]);
    let mut words = [""; 4];
    for (i, word) in words.iter_mut().enumerate() {
        let index = ((bits >> (64 - 11 * (i + 1))) & 0x7ff) as u16;
        *word = language.word(index).unwrap_or("");
    }
    words
}

fn check_value(handshake_hash: &[u8], inviter_nonce: &[u8; 32], joiner_nonce: &[u8; 32]) -> [u8; 32] {
    let ikm = [handshake_hash, inviter_nonce, joiner_nonce].concat();
    *hkdf32(SAS_SALT, &ikm, b"words").expose()
}

fn commitment(nonce: &[u8; 32]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(COMMIT_DOMAIN);
    h.update(nonce);
    h.finalize().into()
}

struct SessionSeeds {
    noise: Zeroizing<[u8; 32]>,
    nonce: [u8; 32],
}

fn seeds(rng: &mut dyn VaultRng) -> Result<SessionSeeds, PairingError> {
    let mut seed = Zeroizing::new([0u8; 32]);
    rng.fill(&mut seed[..]).map_err(|_| PairingError::Rng)?;
    Ok(SessionSeeds {
        noise: Zeroizing::new(*hkdf32(RNG_SALT, &seed[..], b"noise").expose()),
        nonce: *hkdf32(RNG_SALT, &seed[..], b"check-nonce").expose(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PairingState {
    /// Inviter: waiting for the other device. Joiner: not yet connected.
    Waiting,
    /// The handshake is done; show the words.
    WordsReady,
    /// The person confirmed on this device.
    Confirmed,
    Done,
    Aborted,
}

/// The entry point.
pub struct Pairing;

impl Pairing {
    /// Run on a device that is already in the vault. Opens the one invitation
    /// this endpoint accepts (a new one cancels the previous one).
    pub fn invite(endpoint: &LinkEndpoint, ttl: Duration, rng: &mut dyn VaultRng) -> Result<(Invitation, InviterSession), PairingError> {
        if ttl < MIN_TTL || ttl > MAX_TTL {
            return Err(PairingError::State("an invitation lives between 30 seconds and 15 minutes"));
        }
        let mut secret = Zeroizing::new([0u8; SECRET_LEN]);
        rng.fill(&mut secret[..]).map_err(|_| PairingError::Rng)?;
        let seeds = seeds(rng)?;
        let invitation = Invitation::new(endpoint.addr(), endpoint.now() + ttl.as_secs(), *secret);
        let (tx, rx) = oneshot::channel();
        endpoint.open_invite(PendingInvite { id: invitation.id(), expires: invitation.expires, tx });
        let session = InviterSession {
            endpoint: endpoint.clone(),
            invitation: invitation.clone(),
            incoming: Some(rx),
            seeds,
            state: PairingState::Waiting,
            channel: None,
            check: None,
            log: None,
        };
        Ok((invitation, session))
    }

    /// Run on the new device with the scanned invitation. Nothing is sent
    /// until [`JoinerSession::connect`].
    pub fn join(endpoint: &LinkEndpoint, invitation: Invitation, rng: &mut dyn VaultRng) -> Result<JoinerSession, PairingError> {
        let seeds = seeds(rng)?;
        Ok(JoinerSession { endpoint: endpoint.clone(), invitation, seeds, state: PairingState::Waiting, channel: None, check: None, log: None })
    }
}

// ---------------------------------------------------------------- inviter

/// Issues a mandate token for a newly paired device (or declines with `None`).
pub type MandateIssuer = dyn Fn(&DeviceEntry) -> Option<String>;

/// What the inviting device needs to let the new device in.
pub struct InviterContext<'a, S: VaultStore> {
    /// The open vault. A slot is added for the new device.
    pub vault: &'a mut Vault<S>,
    pub records: &'a mut dyn RecordSink,
    pub rng: &'a mut dyn VaultRng,
    /// The role the new device gets on the home.
    pub role: Role,
    /// Issues the new device's own mandate token, if the caller can.
    /// TODO(permissions): until device mandates are issued at pairing, pass
    /// `None`; the new device then has its registry role only.
    pub mandate: Option<&'a MandateIssuer>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InviterOutcome {
    pub device: DeviceEntry,
    /// Whether the new device said it stored the header and opened the vault.
    /// False means the answer was lost; the device is paired all the same.
    pub acknowledged: bool,
    pub recorded: bool,
}

pub struct InviterSession {
    endpoint: LinkEndpoint,
    invitation: Invitation,
    incoming: Option<oneshot::Receiver<PairingConn>>,
    seeds: SessionSeeds,
    state: PairingState,
    channel: Option<SecureChannel>,
    check: Option<[u8; 32]>,
    log: Option<PlaintextLog>,
}

impl Drop for InviterSession {
    fn drop(&mut self) {
        self.endpoint.cancel_invite(&self.invitation.id());
    }
}

impl InviterSession {
    pub fn state(&self) -> PairingState {
        self.state
    }

    pub fn invitation(&self) -> &Invitation {
        &self.invitation
    }

    /// For tests: collect every plaintext this session sends or receives.
    #[cfg(feature = "test-util")]
    pub fn log_plaintext(&mut self, log: PlaintextLog) {
        self.log = Some(log);
    }

    /// For tests: send any message from any state, as a broken or hostile peer would.
    #[cfg(feature = "test-util")]
    pub async fn send_for_tests(&mut self, msg: Msg) -> Result<(), PairingError> {
        self.channel.as_mut().ok_or(PairingError::State("no session"))?.send(&msg).await
    }

    /// Waits for the other device and runs the handshake. Afterwards the
    /// words are ready. Any failure ends the session and the invitation.
    pub async fn accept(&mut self) -> Result<(), PairingError> {
        if self.state != PairingState::Waiting {
            return Err(PairingError::State("accept is the first step"));
        }
        let rx = self.incoming.take().ok_or(PairingError::State("accept is the first step"))?;
        let left = Duration::from_secs(self.invitation.expires.saturating_sub(self.endpoint.now()));
        self.state = PairingState::Aborted;
        let arrived = match timeout(left, rx).await {
            Err(_) => {
                self.endpoint.cancel_invite(&self.invitation.id());
                return Err(PairingError::Expired);
            }
            // The listener gave the invitation up: time ran out, or an attempt failed.
            Ok(Err(_)) if self.endpoint.now() > self.invitation.expires => return Err(PairingError::Expired),
            Ok(Err(_)) => return Err(PairingError::Burned),
            Ok(Ok(conn)) => conn,
        };
        let mut wire = Wire { conn: arrived.conn, send: arrived.send, recv: arrived.recv, log: self.log.clone() };
        if self.endpoint.now() > self.invitation.expires {
            wire.abort_clear(AbortReason::Expired).await;
            return Err(PairingError::Expired);
        }
        let joiner = *wire.conn.remote_id().as_bytes();
        let pro = prologue(&wire.conn, &self.invitation.id(), self.invitation.expires, &self.endpoint.id(), &joiner)?;
        let mut hs = handshake(false, &self.invitation.psk(), &pro, *self.seeds.noise)?;
        let first = wire.read(STEP_TIMEOUT).await?;
        let mut buf = vec![0u8; 1024];
        let read = if first.kind == FrameType::PairNoise { hs.read_message(&first.payload, &mut buf) } else { Err(snow::Error::Input) };
        match read {
            Ok(0) => {}
            _ => {
                // Wrong secret, another connection, or not a handshake at all.
                wire.abort_clear(AbortReason::Handshake).await;
                return Err(PairingError::Handshake);
            }
        }
        let commit = commitment(&self.seeds.nonce);
        log_plaintext(&self.log, &commit);
        let n = hs.write_message(&commit, &mut buf).map_err(|_| PairingError::Protocol("handshake"))?;
        wire.write(FrameType::PairNoise, &buf[..n]).await?;
        let hash = hs.get_handshake_hash().to_vec();
        let transport = hs.into_transport_mode().map_err(|_| PairingError::Protocol("handshake"))?;
        let mut channel = SecureChannel::new(wire, transport);
        let joiner_nonce = match channel.recv(STEP_TIMEOUT).await? {
            Msg::Nonce(n) => n,
            Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
            _ => {
                channel.abort(AbortReason::Protocol).await;
                return Err(PairingError::Protocol("expected the joiner's value"));
            }
        };
        channel.send(&Msg::Reveal(self.seeds.nonce)).await?;
        self.check = Some(check_value(&hash, &self.seeds.nonce, &joiner_nonce));
        self.channel = Some(channel);
        self.state = PairingState::WordsReady;
        Ok(())
    }

    /// The four words to show. Both devices show the same ones, or something is wrong.
    pub fn words(&self, language: Language) -> Result<[&'static str; 4], PairingError> {
        match (self.state, &self.check) {
            (PairingState::WordsReady | PairingState::Confirmed | PairingState::Done, Some(check)) => Ok(check_words(check, language)),
            _ => Err(PairingError::State("the words are ready after the handshake")),
        }
    }

    /// The person saw the same words on the other device and said so here.
    pub async fn confirm(&mut self) -> Result<(), PairingError> {
        if self.state != PairingState::WordsReady {
            return Err(PairingError::State("confirm comes after the words and only once"));
        }
        let channel = self.channel.as_mut().ok_or(PairingError::State("no session"))?;
        self.state = PairingState::Aborted;
        channel.send(&Msg::Confirm).await?;
        self.state = PairingState::Confirmed;
        Ok(())
    }

    /// The person said the words differ, or cancelled. Ends the session.
    pub async fn reject(&mut self, reason: AbortReason) {
        self.endpoint.cancel_invite(&self.invitation.id());
        if let Some(mut channel) = self.channel.take() {
            channel.abort(reason).await;
        }
        self.state = PairingState::Aborted;
    }

    /// Waits for the other side's confirmation, then lets the new device in.
    pub async fn finish<S: VaultStore>(&mut self, ctx: InviterContext<'_, S>) -> Result<InviterOutcome, PairingError> {
        if self.state != PairingState::Confirmed {
            return Err(PairingError::State("finish comes after confirm"));
        }
        let mut channel = self.channel.take().ok_or(PairingError::State("no session"))?;
        self.state = PairingState::Aborted;
        let outcome = inviter_finish(&self.endpoint, &mut channel, ctx).await;
        match &outcome {
            Ok(_) => {
                self.state = PairingState::Done;
                channel.wire.close_after_flush().await;
            }
            Err(PairingError::Aborted(_)) | Err(PairingError::Tampered) | Err(PairingError::Closed(_)) | Err(PairingError::Timeout) => channel.wire.close(),
            Err(PairingError::Vault(_)) | Err(PairingError::Registry(_)) => channel.abort(AbortReason::Refused).await,
            Err(_) => channel.abort(AbortReason::Protocol).await,
        }
        outcome
    }
}

async fn inviter_finish<S: VaultStore>(endpoint: &LinkEndpoint, channel: &mut SecureChannel, ctx: InviterContext<'_, S>) -> Result<InviterOutcome, PairingError> {
    match channel.recv(PERSON_TIMEOUT).await? {
        Msg::Confirm => {}
        Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
        _ => return Err(PairingError::Protocol("expected the other side's confirmation")),
    }
    let vault_id: [u8; 16] = from_hex(ctx.vault.vault_id()).and_then(|v| v.try_into().ok()).ok_or(PairingError::Vault("vault id".into()))?;
    channel.send(&Msg::VaultInfo { vault_id }).await?;
    let enroll = match channel.recv(STEP_TIMEOUT).await? {
        Msg::Enroll(e) => e,
        Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
        _ => return Err(PairingError::Protocol("expected the new device's keys")),
    };
    // The link key is the one this connection authenticated; the device id must vouch for it.
    let link_id = *channel.wire.conn.remote_id().as_bytes();
    if !verify_link_cert(&enroll.device_id, &link_id, &enroll.link_cert) {
        return Err(PairingError::Protocol("the device id does not vouch for this link key"));
    }
    let registry = endpoint.registry().clone();
    if registry.by_device(&enroll.device_id).is_some() || registry.by_link(&link_id).is_some() {
        return Err(PairingError::Registry(RegistryError::Conflict));
    }
    let now = endpoint.now();
    let request = EnrollRequest { label: enroll.label.clone(), public: enroll.slot_public, keystore_blob: enroll.keystore_blob.clone() };
    let slot_id = match ctx.vault.add_device_slot(&request, ctx.rng, now) {
        Ok(id) => id,
        Err(e) => {
            let _ = ctx.records.write(&device_entry(now, ACTION_PAIR, Outcome::Failed, "Pairing a new device failed", &enroll.device_id));
            return Err(PairingError::Vault(e.to_string()));
        }
    };
    let entry = DeviceEntry {
        device_id: enroll.device_id,
        link_id,
        label: enroll.label,
        role: ctx.role,
        state: DeviceState::Active,
        cert: enroll.link_cert,
        paired_at: now,
        slot_id: slot_id.clone(),
    };
    if let Err(e) = registry.add(entry.clone()) {
        // Not in the registry means not on the Link; take the slot back out so the two agree.
        let _ = ctx.vault.remove_device_slot(&slot_id, now);
        return Err(PairingError::Registry(e));
    }
    let header = match ctx.vault.store().header() {
        Ok(Some(bytes)) => bytes,
        _ => return Err(PairingError::Vault("the header could not be read".into())),
    };
    let devices: Vec<DeviceEntry> = registry.list().into_iter().filter(|e| e.state == DeviceState::Active).collect();
    let mandate = ctx.mandate.and_then(|issue| issue(&entry));
    let bootstrap = Bootstrap { vault_id, epoch: ctx.vault.epoch(), slot_id, header, devices, mandate };
    channel.send(&Msg::Bootstrap(Box::new(bootstrap))).await?;
    let acknowledged = matches!(channel.recv(STEP_TIMEOUT).await, Ok(Msg::Done));
    let recorded = ctx.records.write(&device_entry(now, ACTION_PAIR, Outcome::Ok, "Paired a new device", &entry.device_id)).is_ok();
    Ok(InviterOutcome { device: entry, acknowledged, recorded })
}

// ---------------------------------------------------------------- joiner

/// What the new device needs to join.
pub struct JoinerContext<'a> {
    /// The new device's name (1 to 64 bytes, not used by another device).
    pub label: &'a str,
    /// The new device's own keystore: its slot secret is made here and stays here.
    pub keystore: &'a dyn DeviceKeyStore,
    /// The new device's id key (signs its records and vouches for its link key).
    pub signer: &'a dyn Signer,
    /// Stores the header and opens the vault with this device's keystore.
    /// Pairing is acknowledged only if this succeeds.
    pub install: &'a mut dyn FnMut(&Bootstrap) -> Result<(), String>,
    pub records: &'a mut dyn RecordSink,
    pub rng: &'a mut dyn VaultRng,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JoinerOutcome {
    /// Vault id, epoch (remember it: `Vault::check_epoch`), slot id, header, devices, mandate.
    pub bootstrap: Bootstrap,
    /// The device that let this one in.
    pub inviter: DeviceEntry,
    pub recorded: bool,
}

pub struct JoinerSession {
    endpoint: LinkEndpoint,
    invitation: Invitation,
    seeds: SessionSeeds,
    state: PairingState,
    channel: Option<SecureChannel>,
    check: Option<[u8; 32]>,
    log: Option<PlaintextLog>,
}

impl JoinerSession {
    pub fn state(&self) -> PairingState {
        self.state
    }

    /// For tests: collect every plaintext this session sends or receives.
    #[cfg(feature = "test-util")]
    pub fn log_plaintext(&mut self, log: PlaintextLog) {
        self.log = Some(log);
    }

    /// For tests: send any message from any state, as a broken or hostile peer would.
    #[cfg(feature = "test-util")]
    pub async fn send_for_tests(&mut self, msg: Msg) -> Result<(), PairingError> {
        self.channel.as_mut().ok_or(PairingError::State("no session"))?.send(&msg).await
    }

    /// For tests: read the next message, whatever it is.
    #[cfg(feature = "test-util")]
    pub async fn recv_for_tests(&mut self) -> Result<Msg, PairingError> {
        self.channel.as_mut().ok_or(PairingError::State("no session"))?.recv(STEP_TIMEOUT).await
    }

    /// Dials the inviting device and runs the handshake. Afterwards the words are ready.
    pub async fn connect(&mut self) -> Result<(), PairingError> {
        if self.state != PairingState::Waiting {
            return Err(PairingError::State("connect is the first step"));
        }
        self.state = PairingState::Aborted;
        if self.endpoint.now() > self.invitation.expires {
            return Err(PairingError::Expired);
        }
        let conn = self.endpoint.dial(&self.invitation.addr, PAIR_ALPN).await?;
        // iroh only completes a connection to the key that was dialled; checked again because everything rests on it.
        if conn.remote_id().as_bytes() != &self.invitation.addr.id {
            return Err(PairingError::Protocol("connected to another device"));
        }
        let (send, recv) = conn.open_bi().await.map_err(|e| PairingError::Closed(e.to_string()))?;
        let mut wire = Wire { conn, send, recv, log: self.log.clone() };
        let id = self.invitation.id();
        let mut hello = vec![PAIR_VERSION];
        hello.extend_from_slice(&id);
        log_plaintext(&self.log, &hello);
        wire.write(FrameType::PairHello, &hello).await?;
        let pro = prologue(&wire.conn, &id, self.invitation.expires, &self.invitation.addr.id, &self.endpoint.id())?;
        let mut hs = handshake(true, &self.invitation.psk(), &pro, *self.seeds.noise)?;
        let mut buf = vec![0u8; 1024];
        let n = hs.write_message(&[], &mut buf).map_err(|_| PairingError::Protocol("handshake"))?;
        wire.write(FrameType::PairNoise, &buf[..n]).await?;
        let reply = match wire.read(STEP_TIMEOUT).await {
            Ok(frame) => frame,
            // The inviter closes on a failed handshake; the reason it gave (if it arrived) is the better error.
            Err(e) => return Err(e),
        };
        let commit: [u8; 32] = match reply.kind {
            FrameType::PairAbort => {
                wire.close();
                return Err(match AbortReason::from_u8(reply.payload.first().copied().unwrap_or(0)) {
                    AbortReason::Expired => PairingError::Expired,
                    AbortReason::UnknownInvite => PairingError::Burned,
                    AbortReason::Handshake => PairingError::Handshake,
                    other => PairingError::Aborted(other),
                });
            }
            FrameType::PairNoise => match hs.read_message(&reply.payload, &mut buf) {
                Ok(32) => {
                    let mut c = [0u8; 32];
                    c.copy_from_slice(&buf[..32]);
                    c
                }
                _ => {
                    wire.close();
                    return Err(PairingError::Handshake);
                }
            },
            _ => {
                wire.close();
                return Err(PairingError::Protocol("expected the handshake reply"));
            }
        };
        log_plaintext(&self.log, &commit);
        let hash = hs.get_handshake_hash().to_vec();
        let transport = hs.into_transport_mode().map_err(|_| PairingError::Protocol("handshake"))?;
        let mut channel = SecureChannel::new(wire, transport);
        channel.send(&Msg::Nonce(self.seeds.nonce)).await?;
        let inviter_nonce = match channel.recv(STEP_TIMEOUT).await? {
            Msg::Reveal(n) => n,
            Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
            _ => {
                channel.abort(AbortReason::Protocol).await;
                return Err(PairingError::Protocol("expected the inviter's value"));
            }
        };
        if !muse_core::vault::secret::ct_eq(&commitment(&inviter_nonce), &commit) {
            channel.abort(AbortReason::Protocol).await;
            return Err(PairingError::Protocol("the inviter's value does not match its commitment"));
        }
        self.check = Some(check_value(&hash, &inviter_nonce, &self.seeds.nonce));
        self.channel = Some(channel);
        self.state = PairingState::WordsReady;
        Ok(())
    }

    pub fn words(&self, language: Language) -> Result<[&'static str; 4], PairingError> {
        match (self.state, &self.check) {
            (PairingState::WordsReady | PairingState::Confirmed | PairingState::Done, Some(check)) => Ok(check_words(check, language)),
            _ => Err(PairingError::State("the words are ready after the handshake")),
        }
    }

    pub async fn confirm(&mut self) -> Result<(), PairingError> {
        if self.state != PairingState::WordsReady {
            return Err(PairingError::State("confirm comes after the words and only once"));
        }
        let channel = self.channel.as_mut().ok_or(PairingError::State("no session"))?;
        self.state = PairingState::Aborted;
        channel.send(&Msg::Confirm).await?;
        self.state = PairingState::Confirmed;
        Ok(())
    }

    pub async fn reject(&mut self, reason: AbortReason) {
        if let Some(mut channel) = self.channel.take() {
            channel.abort(reason).await;
        }
        self.state = PairingState::Aborted;
    }

    /// Waits for the inviting side's confirmation, sends this device's public
    /// keys, and receives what it needs to open the vault.
    pub async fn finish(&mut self, ctx: JoinerContext<'_>) -> Result<JoinerOutcome, PairingError> {
        if self.state != PairingState::Confirmed {
            return Err(PairingError::State("finish comes after confirm"));
        }
        let mut channel = self.channel.take().ok_or(PairingError::State("no session"))?;
        self.state = PairingState::Aborted;
        let outcome = joiner_finish(&self.endpoint, &mut channel, ctx).await;
        match &outcome {
            Ok(_) => {
                self.state = PairingState::Done;
                channel.wire.close_after_flush().await;
            }
            Err(PairingError::Aborted(_)) | Err(PairingError::Tampered) | Err(PairingError::Closed(_)) | Err(PairingError::Timeout) => channel.wire.close(),
            Err(PairingError::Install(_)) | Err(PairingError::Registry(_)) | Err(PairingError::Vault(_)) => channel.abort(AbortReason::Unusable).await,
            Err(_) => channel.abort(AbortReason::Protocol).await,
        }
        outcome
    }
}

async fn joiner_finish(endpoint: &LinkEndpoint, channel: &mut SecureChannel, ctx: JoinerContext<'_>) -> Result<JoinerOutcome, PairingError> {
    match channel.recv(PERSON_TIMEOUT).await? {
        Msg::Confirm => {}
        Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
        _ => return Err(PairingError::Protocol("expected the other side's confirmation")),
    }
    let vault_id = match channel.recv(STEP_TIMEOUT).await? {
        Msg::VaultInfo { vault_id } => vault_id,
        Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
        _ => return Err(PairingError::Protocol("expected the vault id")),
    };
    let vault_hex = to_hex(&vault_id);
    let device_id = device_id_of(ctx.signer).ok_or(PairingError::Protocol("device id"))?;
    let my_link = endpoint.id();
    let link_cert = certify(ctx.signer, &my_link).ok_or(PairingError::Protocol("device id"))?;
    // The slot secret is made here and goes into this device's keystore; only the public half leaves.
    let request = Vault::<MemoryStore>::enroll_device(&vault_hex, ctx.label, ctx.keystore, ctx.rng).map_err(|e| PairingError::Vault(e.to_string()))?;
    channel
        .send(&Msg::Enroll(Enroll {
            label: request.label.clone(),
            slot_public: request.public,
            keystore_blob: request.keystore_blob.clone(),
            device_id,
            link_cert,
        }))
        .await?;
    let bootstrap = match channel.recv(STEP_TIMEOUT).await? {
        Msg::Bootstrap(b) => *b,
        Msg::Abort(reason) => return Err(PairingError::Aborted(reason)),
        _ => return Err(PairingError::Protocol("expected the vault header")),
    };
    let inviter_link = *channel.wire.conn.remote_id().as_bytes();
    let inviter = check_bootstrap(&bootstrap, &vault_id, &request, &device_id, &my_link, &inviter_link)?;
    (ctx.install)(&bootstrap).map_err(PairingError::Install)?;
    let registry = endpoint.registry();
    for entry in &bootstrap.devices {
        registry.add(entry.clone()).map_err(PairingError::Registry)?;
    }
    let recorded = ctx.records.write(&device_entry(endpoint.now(), ACTION_PAIR, Outcome::Ok, "Joined the vault as a new device", &device_id)).is_ok();
    channel.send(&Msg::Done).await?;
    Ok(JoinerOutcome { bootstrap, inviter, recorded })
}

/// The new device's own checks on what it was sent. It trusts the inviting
/// device (the person confirmed it), but not blindly: the header must be for
/// the vault it enrolled in and hold exactly the slot it asked for, and the
/// device list must be consistent with the connection it arrived on.
fn check_bootstrap(b: &Bootstrap, vault_id: &[u8; 16], request: &EnrollRequest, device_id: &DeviceId, my_link: &[u8; 32], inviter_link: &[u8; 32]) -> Result<DeviceEntry, PairingError> {
    if &b.vault_id != vault_id {
        return Err(PairingError::Protocol("another vault"));
    }
    let header = Header::parse(&b.header).map_err(|_| PairingError::Protocol("the header does not parse"))?;
    if header.vault_id != to_hex(vault_id) || header.epoch != b.epoch {
        return Err(PairingError::Protocol("the header is for another vault or epoch"));
    }
    let slot = header.slots.iter().find(|s| s.id == b.slot_id).ok_or(PairingError::Protocol("no slot for this device"))?;
    if slot.kind != "device" || slot.public != to_hex(&request.public) || slot.label != request.label || slot.keystore_blob != to_hex(&request.keystore_blob) {
        return Err(PairingError::Protocol("the slot is not the one this device asked for"));
    }
    if b.devices.iter().any(|e| !e.cert_ok()) {
        return Err(PairingError::Protocol("a device in the list has a bad certificate"));
    }
    let me = b.devices.iter().find(|e| &e.device_id == device_id).ok_or(PairingError::Protocol("this device is not in the list"))?;
    if &me.link_id != my_link || me.slot_id != b.slot_id {
        return Err(PairingError::Protocol("this device is listed with other keys"));
    }
    let inviter = b.devices.iter().find(|e| &e.link_id == inviter_link).ok_or(PairingError::Protocol("the inviting device is not in the list"))?;
    Ok(inviter.clone())
}

// ---------------------------------------------------------------- lost device

#[derive(Debug)]
pub struct LostDevice {
    pub device: DeviceEntry,
    /// What the vault did: new epoch, re-wrapped heads, new recovery words if any (show them).
    pub rotation: Rotation,
    /// Open Link connections from the device that were closed.
    pub closed_connections: usize,
    pub recorded: bool,
}

/// "I lost this device", run on any other device of the person.
///
/// In this order, so that a failure half-way leaves the device shut out
/// rather than let in: the registry entry becomes `removed` and its open
/// connections are closed (from here the Link refuses it); the vault removes
/// its slot and rotates the master key; a `device.remove` record is written.
///
/// What this cannot do: anything the lost device had already copied stays
/// readable to whoever holds it (vault-format section 9.1).
pub fn lost_device<S: VaultStore>(
    endpoint: &LinkEndpoint,
    vault: &mut Vault<S>,
    device_id: &DeviceId,
    policy: RecoveryPolicy,
    records: &mut dyn RecordSink,
    rng: &mut dyn VaultRng,
) -> Result<LostDevice, PairingError> {
    let registry = endpoint.registry().clone();
    let known = registry.by_device(device_id).ok_or(PairingError::Registry(RegistryError::NotFound))?;
    if known.link_id == endpoint.id() {
        return Err(PairingError::State("a device cannot remove itself; do it from another device"));
    }
    let device = registry.remove(device_id).map_err(PairingError::Registry)?;
    let closed_connections = endpoint.disconnect(&device.link_id);
    let now = endpoint.now();
    let rotation = match vault.lost_device(&device.slot_id, policy, rng, now) {
        Ok(rotation) => rotation,
        Err(e) => {
            let _ = records.write(&device_entry(now, ACTION_REMOVE, Outcome::Failed, "Removing a device from the vault failed; it is cut off from the link", device_id));
            return Err(PairingError::Vault(e.to_string()));
        }
    };
    let recorded = records.write(&device_entry(now, ACTION_REMOVE, Outcome::Ok, "Removed a device and changed the vault key", device_id)).is_ok();
    Ok(LostDevice { device, rotation, closed_connections, recorded })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn four_words_use_the_first_44_bits() {
        let mut value = [0u8; 32];
        assert_eq!(check_words(&value, Language::English), ["abandon"; 4]);
        value[..6].copy_from_slice(&[0xff; 6]);
        assert_eq!(check_words(&value, Language::English)[..3], ["zoo"; 3]);
        // The 45th bit and beyond do not matter.
        let mut other = [0u8; 32];
        other[5] = 0x0f;
        other[6] = 0xff;
        assert_eq!(check_words(&other, Language::English), ["abandon"; 4]);
        other[5] = 0x10;
        assert_ne!(check_words(&other, Language::English), ["abandon"; 4]);
        for language in Language::ALL {
            assert!(check_words(&[0x5a; 32], language).iter().all(|w| !w.is_empty()));
        }
    }

    #[test]
    fn check_value_depends_on_every_input() {
        let base = check_value(b"hash", &[1; 32], &[2; 32]);
        assert_ne!(base, check_value(b"hasi", &[1; 32], &[2; 32]));
        assert_ne!(base, check_value(b"hash", &[3; 32], &[2; 32]));
        assert_ne!(base, check_value(b"hash", &[1; 32], &[3; 32]));
        assert_ne!(base, check_value(b"hash", &[2; 32], &[1; 32]));
        assert_ne!(commitment(&[1; 32]), commitment(&[2; 32]));
    }
}
