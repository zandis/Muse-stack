//! The Link: one iroh endpoint per device, a registry of paired devices, and
//! a small request/response protocol over one ALPN.
//!
//! Limits, all enforced here and none unbounded:
//!
//! | What | Default | Where |
//! |---|---|---|
//! | Connections in total / per device | 64 / 4 | accept path |
//! | Streams open at once per connection | 16 | QUIC flow control |
//! | Requests per device | 20 a second, burst 40 | token bucket |
//! | Time to send a whole request | 10 s | per stream |
//! | Time for the handler to answer | 30 s | per stream |
//! | Request / response body | 1 MiB / 8 MiB | frame length check before reading |
//! | Idle connection | closed after 60 s; keep-alive every 15 s | QUIC |

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock, Weak};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use iroh::address_lookup::{DnsAddressLookup, PkarrPublisher, PkarrResolver};
use iroh::endpoint::{presets, Connection, ConnectionError, Incoming, QuicTransportConfig, RecvStream, SendStream, VarInt};
use iroh::{Endpoint, EndpointAddr, PublicKey, RelayMode, RelayUrl, SecretKey, TransportAddr};
use muse_core::vault::{VaultError, VaultRng};
use tokio::sync::{oneshot, Semaphore};
use tokio::time::{timeout, Instant};
use zeroize::Zeroizing;

use crate::error::LinkError;
use crate::frame::{
    self, read_frame, read_frame_or_end, write_frame, Cursor, Frame, FrameError, FrameType, Refusal, RefusalCode, Request, Response,
    MAX_SYNC_MESSAGE, MAX_SYNC_NAME,
};
use crate::handler::{LinkHandler, Peer};
use crate::registry::{active_peer, DeviceEntry, DeviceId, DeviceRegistry, LinkId, RegistryError};

/// The application protocol: home API calls and sync channels. Paired devices only.
pub const LINK_ALPN: &[u8] = b"muse/link/1";
/// The pairing protocol. Offered only while an invitation is open.
pub const PAIR_ALPN: &[u8] = b"muse/pair/1";

/// QUIC application close codes (ASCII "M" then a number).
pub const CLOSE_NORMAL: u32 = 0;
pub const CLOSE_NOT_PAIRED: u32 = 0x4d01;
pub const CLOSE_REMOVED: u32 = 0x4d02;
pub const CLOSE_BUSY: u32 = 0x4d03;
pub const CLOSE_PAIRING: u32 = 0x4d10;

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const PAIR_HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_PAIR_CONNECTIONS: usize = 4;

/// Unix seconds. Injected so tests can move time.
pub type Clock = Arc<dyn Fn() -> u64 + Send + Sync>;

pub fn system_clock() -> Clock {
    Arc::new(|| SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0))
}

/// The secret key of a device's iroh endpoint. Separate from the device id
/// (see `registry`); 32 bytes of Ed25519 seed.
pub struct LinkKey(Zeroizing<[u8; 32]>);

impl LinkKey {
    pub fn from_bytes(bytes: [u8; 32]) -> LinkKey {
        LinkKey(Zeroizing::new(bytes))
    }

    pub fn generate(rng: &mut dyn VaultRng) -> Result<LinkKey, VaultError> {
        let mut bytes = Zeroizing::new([0u8; 32]);
        rng.fill(&mut bytes[..])?;
        Ok(LinkKey(bytes))
    }

    /// The link id: the public key other devices dial.
    pub fn public(&self) -> LinkId {
        *SecretKey::from_bytes(&self.0).public().as_bytes()
    }

    /// For the device's keystore. Treat as a secret.
    pub fn expose(&self) -> &[u8; 32] {
        &self.0
    }
}

impl std::fmt::Debug for LinkKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LinkKey(..)")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RelayChoice {
    /// iroh's public relays (rate-limited; for development and small use).
    Default,
    /// These relays only, e.g. `https://relay.example.org`.
    Custom(Vec<String>),
    /// No relay: only directly reachable addresses work.
    None,
}

/// What the pairing listener does with a failed attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PairingPolicy {
    /// Cancel the open invitation on ANY failed attempt on the pairing
    /// protocol, including one that does not name it. True (the default)
    /// means nobody gets a second try at an invitation; the cost is that
    /// someone who knows this device's link id can cancel an open invitation
    /// (the person then shows a new code).
    pub burn_on_any_failed_attempt: bool,
}

#[derive(Clone)]
pub struct LinkConfig {
    pub relay: RelayChoice,
    /// Publish and look up addresses through iroh's DNS/pkarr service, so a
    /// device can be dialled by key alone. Off: addresses must be given.
    pub discovery: bool,
    /// Bind this address only (tests: `127.0.0.1:0`). `None`: iroh's default (all interfaces).
    pub bind: Option<SocketAddr>,
    /// Send relay and discovery HTTPS through the proxy named in the environment.
    pub proxy_from_env: bool,
    pub idle_timeout: Duration,
    pub keep_alive: Duration,
    pub max_streams_per_connection: u32,
    pub max_connections_per_peer: usize,
    pub max_connections: usize,
    pub requests_per_second: u32,
    pub burst: u32,
    pub request_read_timeout: Duration,
    pub handler_timeout: Duration,
    /// Client: how long to wait for an answer.
    pub response_timeout: Duration,
    pub connect_timeout: Duration,
    pub connect_attempts: u32,
    pub backoff_min: Duration,
    pub backoff_max: Duration,
    pub pairing: PairingPolicy,
    pub clock: Clock,
}

impl Default for LinkConfig {
    /// For real use: public relays and address lookup on.
    fn default() -> Self {
        LinkConfig {
            relay: RelayChoice::Default,
            discovery: true,
            bind: None,
            proxy_from_env: false,
            idle_timeout: Duration::from_secs(60),
            keep_alive: Duration::from_secs(15),
            max_streams_per_connection: 16,
            max_connections_per_peer: 4,
            max_connections: 64,
            requests_per_second: 20,
            burst: 40,
            request_read_timeout: Duration::from_secs(10),
            handler_timeout: Duration::from_secs(30),
            response_timeout: Duration::from_secs(60),
            connect_timeout: Duration::from_secs(15),
            connect_attempts: 3,
            backoff_min: Duration::from_millis(200),
            backoff_max: Duration::from_secs(30),
            pairing: PairingPolicy { burn_on_any_failed_attempt: true },
            clock: system_clock(),
        }
    }
}

impl LinkConfig {
    /// No relay, no address lookup, loopback only: nothing leaves the machine.
    pub fn offline() -> Self {
        LinkConfig {
            relay: RelayChoice::None,
            discovery: false,
            bind: Some(SocketAddr::from(([127, 0, 0, 1], 0))),
            ..LinkConfig::default()
        }
    }
}

impl std::fmt::Debug for LinkConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LinkConfig").field("relay", &self.relay).field("discovery", &self.discovery).field("bind", &self.bind).finish_non_exhaustive()
    }
}

/// How to reach a device: its link id and, when address lookup is off or as a
/// hint, a relay and direct addresses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkAddr {
    pub id: LinkId,
    pub relay: Option<String>,
    pub direct: Vec<SocketAddr>,
}

impl LinkAddr {
    pub fn new(id: LinkId) -> LinkAddr {
        LinkAddr { id, relay: None, direct: Vec::new() }
    }

    pub(crate) fn to_iroh(&self) -> Result<EndpointAddr, LinkError> {
        let key = PublicKey::from_bytes(&self.id).map_err(|_| LinkError::Config("the link id is not a valid key".into()))?;
        let mut addrs: Vec<TransportAddr> = self.direct.iter().copied().map(TransportAddr::Ip).collect();
        if let Some(relay) = &self.relay {
            let url: RelayUrl = relay.parse().map_err(|_| LinkError::Config("the relay address is not a URL".into()))?;
            addrs.push(TransportAddr::Relay(url));
        }
        Ok(EndpointAddr::from_parts(key, addrs))
    }

    pub(crate) fn from_iroh(addr: &EndpointAddr) -> LinkAddr {
        LinkAddr {
            id: *addr.id.as_bytes(),
            relay: addr.relay_urls().next().map(|u| u.to_string()),
            direct: addr.ip_addrs().copied().collect(),
        }
    }
}

/// A pairing connection whose first frame named the open invitation.
pub(crate) struct PairingConn {
    pub conn: Connection,
    pub send: SendStream,
    pub recv: RecvStream,
}

pub(crate) struct PendingInvite {
    pub id: [u8; 8],
    /// Unix seconds.
    pub expires: u64,
    pub tx: oneshot::Sender<PairingConn>,
}

struct Bucket {
    tokens: f64,
    last: Instant,
}

#[derive(Default)]
struct PeerState {
    conns: Vec<Connection>,
    bucket: Option<Bucket>,
}

struct Inner {
    ep: Endpoint,
    cfg: LinkConfig,
    registry: Arc<dyn DeviceRegistry>,
    handler: RwLock<Option<Arc<dyn LinkHandler>>>,
    peers: Mutex<HashMap<LinkId, PeerState>>,
    connections: AtomicUsize,
    invite: Mutex<Option<PendingInvite>>,
    pair_slots: Semaphore,
    accept_task: Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl Drop for Inner {
    fn drop(&mut self) {
        if let Ok(mut task) = self.accept_task.lock() {
            if let Some(task) = task.take() {
                task.abort();
            }
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// A device's end of the Link.
#[derive(Clone)]
pub struct LinkEndpoint {
    inner: Arc<Inner>,
}

impl std::fmt::Debug for LinkEndpoint {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "LinkEndpoint({})", muse_core::records::to_hex(&self.id()))
    }
}

impl LinkEndpoint {
    /// Starts the endpoint. It accepts nothing until [`LinkEndpoint::serve`]
    /// is called or an invitation is opened: with no protocol offered, the TLS
    /// handshake of any incoming connection fails.
    pub async fn bind(key: &LinkKey, config: LinkConfig, registry: Arc<dyn DeviceRegistry>) -> Result<LinkEndpoint, LinkError> {
        let idle = config.idle_timeout.try_into().map_err(|_| LinkError::Config("idle timeout too long".into()))?;
        let transport = QuicTransportConfig::builder()
            .max_concurrent_bidi_streams(VarInt::from_u32(config.max_streams_per_connection))
            .max_concurrent_uni_streams(VarInt::from_u32(0))
            .max_idle_timeout(Some(idle))
            .keep_alive_interval(config.keep_alive)
            .build();
        let relay = match &config.relay {
            RelayChoice::None => RelayMode::Disabled,
            RelayChoice::Default => RelayMode::Default,
            RelayChoice::Custom(urls) => {
                let mut parsed = Vec::new();
                for url in urls {
                    parsed.push(url.parse::<RelayUrl>().map_err(|_| LinkError::Config(format!("not a relay URL: {url}")))?);
                }
                if parsed.is_empty() {
                    return Err(LinkError::Config("no relay given".into()));
                }
                RelayMode::custom(parsed)
            }
        };
        let mut builder = Endpoint::builder(presets::Minimal)
            .secret_key(SecretKey::from_bytes(key.expose()))
            .transport_config(transport)
            .relay_mode(relay)
            .clear_address_lookup();
        if config.discovery {
            builder = builder
                .address_lookup(PkarrPublisher::n0_dns())
                .address_lookup(PkarrResolver::n0_dns())
                .address_lookup(DnsAddressLookup::n0_dns());
        }
        if let Some(addr) = config.bind {
            builder = builder.clear_ip_transports().bind_addr(addr).map_err(|e| LinkError::Bind(e.to_string()))?;
        }
        if config.proxy_from_env {
            builder = builder.proxy_from_env();
        }
        let ep = builder.bind().await.map_err(|e| LinkError::Bind(e.to_string()))?;
        let inner = Arc::new(Inner {
            ep: ep.clone(),
            cfg: config,
            registry,
            handler: RwLock::new(None),
            peers: Mutex::new(HashMap::new()),
            connections: AtomicUsize::new(0),
            invite: Mutex::new(None),
            pair_slots: Semaphore::new(MAX_PAIR_CONNECTIONS),
            accept_task: Mutex::new(None),
        });
        let weak = Arc::downgrade(&inner);
        let task = tokio::spawn(accept_loop(ep, weak));
        *lock(&inner.accept_task) = Some(task);
        Ok(LinkEndpoint { inner })
    }

    /// This device's link id.
    pub fn id(&self) -> LinkId {
        *self.inner.ep.id().as_bytes()
    }

    /// The address to hand to another device (in an invitation, or to a client).
    pub fn addr(&self) -> LinkAddr {
        LinkAddr::from_iroh(&self.inner.ep.addr())
    }

    /// Waits until the endpoint has a relay connection (when relays are on).
    pub async fn online(&self) {
        self.inner.ep.online().await;
    }

    pub fn config(&self) -> &LinkConfig {
        &self.inner.cfg
    }

    pub fn registry(&self) -> &Arc<dyn DeviceRegistry> {
        &self.inner.registry
    }

    pub(crate) fn now(&self) -> u64 {
        (self.inner.cfg.clock)()
    }

    /// Starts serving the application protocol to paired devices.
    pub fn serve(&self, handler: Arc<dyn LinkHandler>) {
        *self.inner.handler.write().unwrap_or_else(|p| p.into_inner()) = Some(handler);
        self.inner.refresh_alpns();
    }

    /// A client for calling another device.
    pub fn client(&self, addr: LinkAddr) -> Result<LinkClient, LinkError> {
        Ok(LinkClient {
            ep: self.inner.ep.clone(),
            addr: addr.to_iroh()?,
            cfg: self.inner.cfg.clone(),
            state: tokio::sync::Mutex::new(ClientState { conn: None, failures: 0, not_before: None }),
        })
    }

    /// Closes every open connection from this link id (code: removed).
    pub fn disconnect(&self, link_id: &LinkId) -> usize {
        let conns = lock(&self.inner.peers).remove(link_id).map(|p| p.conns).unwrap_or_default();
        for conn in &conns {
            conn.close(VarInt::from_u32(CLOSE_REMOVED), b"removed");
        }
        conns.len()
    }

    /// Marks a device removed in the registry and closes its connections.
    /// From then on the Link refuses it. (The vault side of "I lost this
    /// device" is [`crate::pairing::lost_device`].)
    pub fn remove_device(&self, device_id: &DeviceId) -> Result<DeviceEntry, RegistryError> {
        let entry = self.inner.registry.remove(device_id)?;
        self.disconnect(&entry.link_id);
        Ok(entry)
    }

    /// How many connections from paired devices are open.
    pub fn open_connections(&self) -> usize {
        lock(&self.inner.peers).values().map(|p| p.conns.len()).sum()
    }

    /// Closes the endpoint and every connection.
    pub async fn close(&self) {
        if let Some(task) = lock(&self.inner.accept_task).take() {
            task.abort();
        }
        lock(&self.inner.invite).take();
        self.inner.ep.close().await;
    }

    // ---- for the pairing module

    pub(crate) fn open_invite(&self, invite: PendingInvite) {
        // One invitation at a time: a new one cancels the old.
        *lock(&self.inner.invite) = Some(invite);
        self.inner.refresh_alpns();
    }

    pub(crate) fn cancel_invite(&self, id: &[u8; 8]) {
        let mut slot = lock(&self.inner.invite);
        if slot.as_ref().is_some_and(|p| &p.id == id) {
            slot.take();
        }
        drop(slot);
        self.inner.refresh_alpns();
    }

    pub(crate) async fn dial(&self, addr: &LinkAddr, alpn: &[u8]) -> Result<Connection, LinkError> {
        let target = addr.to_iroh()?;
        match timeout(self.inner.cfg.connect_timeout, self.inner.ep.connect(target, alpn)).await {
            Ok(Ok(conn)) => Ok(conn),
            Ok(Err(e)) => Err(LinkError::Unreachable(e.to_string())),
            Err(_) => Err(LinkError::Unreachable("timed out".into())),
        }
    }
}

impl Inner {
    fn refresh_alpns(&self) {
        let mut alpns = Vec::new();
        if self.handler.read().unwrap_or_else(|p| p.into_inner()).is_some() {
            alpns.push(LINK_ALPN.to_vec());
        }
        if lock(&self.invite).is_some() {
            alpns.push(PAIR_ALPN.to_vec());
        }
        self.ep.set_alpns(alpns);
    }

    /// One request's worth from this peer's bucket.
    fn take_token(&self, link_id: &LinkId) -> bool {
        let rate = self.cfg.requests_per_second.max(1) as f64;
        let burst = self.cfg.burst.max(1) as f64;
        let now = Instant::now();
        let mut peers = lock(&self.peers);
        let peer = peers.entry(*link_id).or_default();
        let bucket = peer.bucket.get_or_insert(Bucket { tokens: burst, last: now });
        bucket.tokens = (bucket.tokens + now.duration_since(bucket.last).as_secs_f64() * rate).min(burst);
        bucket.last = now;
        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

struct ConnectionCount(Arc<Inner>);

impl Drop for ConnectionCount {
    fn drop(&mut self) {
        self.0.connections.fetch_sub(1, Ordering::SeqCst);
    }
}

async fn accept_loop(ep: Endpoint, weak: Weak<Inner>) {
    while let Some(incoming) = ep.accept().await {
        let Some(inner) = weak.upgrade() else { break };
        if inner.connections.fetch_add(1, Ordering::SeqCst) >= inner.cfg.max_connections {
            inner.connections.fetch_sub(1, Ordering::SeqCst);
            incoming.refuse();
            continue;
        }
        let count = ConnectionCount(inner.clone());
        tokio::spawn(async move {
            on_incoming(inner, incoming).await;
            drop(count);
        });
    }
}

async fn on_incoming(inner: Arc<Inner>, incoming: Incoming) {
    let Ok(mut accepting) = incoming.accept() else { return };
    let Ok(Ok(alpn)) = timeout(HANDSHAKE_TIMEOUT, accepting.alpn()).await else { return };
    let Ok(Ok(conn)) = timeout(HANDSHAKE_TIMEOUT, accepting).await else { return };
    // From here the peer's key is authenticated by the TLS handshake.
    if alpn == LINK_ALPN {
        link_connection(inner, conn).await;
    } else if alpn == PAIR_ALPN {
        pair_connection(inner, conn).await;
    } else {
        conn.close(VarInt::from_u32(CLOSE_NOT_PAIRED), b"protocol");
    }
}

async fn link_connection(inner: Arc<Inner>, conn: Connection) {
    let link_id = *conn.remote_id().as_bytes();
    // Refused before any stream is accepted: an unknown or removed key never
    // gets an application byte read.
    if active_peer(inner.registry.as_ref(), &link_id).is_none() {
        conn.close(VarInt::from_u32(CLOSE_NOT_PAIRED), b"not paired");
        return;
    }
    {
        let mut peers = lock(&inner.peers);
        let peer = peers.entry(link_id).or_default();
        peer.conns.retain(|c| c.close_reason().is_none());
        if peer.conns.len() >= inner.cfg.max_connections_per_peer {
            drop(peers);
            conn.close(VarInt::from_u32(CLOSE_BUSY), b"too many connections");
            return;
        }
        peer.conns.push(conn.clone());
    }
    while let Ok((send, recv)) = conn.accept_bi().await {
        tokio::spawn(serve_stream(inner.clone(), conn.clone(), link_id, send, recv));
    }
    let id = conn.stable_id();
    if let Some(peer) = lock(&inner.peers).get_mut(&link_id) {
        peer.conns.retain(|c| c.stable_id() != id);
    }
}

async fn refuse(send: &mut SendStream, code: RefusalCode, text: &str) {
    let _ = write_frame(send, FrameType::Refusal, &Refusal::new(code, text).encode()).await;
    let _ = send.finish();
}

async fn serve_stream(inner: Arc<Inner>, conn: Connection, link_id: LinkId, mut send: SendStream, mut recv: RecvStream) {
    // The registry is asked again for every stream: a device removed while its
    // connection is open is cut off here even if nobody called `disconnect`.
    let Some(entry) = active_peer(inner.registry.as_ref(), &link_id) else {
        refuse(&mut send, RefusalCode::Removed, "this device was removed").await;
        conn.close(VarInt::from_u32(CLOSE_REMOVED), b"removed");
        return;
    };
    let peer = Peer::from_entry(&entry);
    let first = match timeout(inner.cfg.request_read_timeout, read_frame(&mut recv)).await {
        Err(_) => {
            let _ = recv.stop(VarInt::from_u32(RefusalCode::Timeout as u32));
            refuse(&mut send, RefusalCode::Timeout, "the request did not arrive in time").await;
            return;
        }
        Ok(Err(e)) => {
            let _ = recv.stop(VarInt::from_u32(RefusalCode::BadRequest as u32));
            refuse(&mut send, RefusalCode::BadRequest, &e.to_string()).await;
            return;
        }
        Ok(Ok(frame)) => frame,
    };
    if !inner.take_token(&link_id) {
        refuse(&mut send, RefusalCode::RateLimited, "too many requests").await;
        return;
    }
    let handler = inner.handler.read().unwrap_or_else(|p| p.into_inner()).clone();
    let Some(handler) = handler else {
        refuse(&mut send, RefusalCode::NoHandler, "nothing is serving here").await;
        return;
    };
    match first.kind {
        FrameType::Request => {
            let request = match Request::decode(&first.payload) {
                Ok(r) => r,
                Err(e) => return refuse(&mut send, RefusalCode::BadRequest, &e.to_string()).await,
            };
            let response = match timeout(inner.cfg.handler_timeout, handler.handle(&peer, request)).await {
                Ok(r) => r,
                Err(_) => return refuse(&mut send, RefusalCode::Timeout, "the handler did not answer in time").await,
            };
            match response.encode() {
                Ok(payload) => {
                    let _ = write_frame(&mut send, FrameType::Response, &payload).await;
                    let _ = send.finish();
                }
                Err(_) => refuse(&mut send, RefusalCode::Internal, "the answer could not be sent").await,
            }
        }
        FrameType::SyncOpen => {
            let name = match parse_sync_open(&first.payload) {
                Ok(n) => n,
                Err(e) => return refuse(&mut send, RefusalCode::BadRequest, &e.to_string()).await,
            };
            if write_frame(&mut send, FrameType::SyncAccept, &[]).await.is_err() {
                return;
            }
            handler.sync(&peer, SyncChannel { name, send, recv, closed: false }).await;
        }
        _ => refuse(&mut send, RefusalCode::BadRequest, "a stream starts with a request or a sync open").await,
    }
}

fn parse_sync_open(payload: &[u8]) -> Result<String, FrameError> {
    let mut c = Cursor::new(payload);
    let name = Cursor::text(c.lp8(MAX_SYNC_NAME)?)?.to_string();
    c.end()?;
    let ok = !name.is_empty() && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-/".contains(&b));
    if ok {
        Ok(name)
    } else {
        Err(FrameError::Malformed("sync channel name"))
    }
}

/// Pairing abort codes sent in clear before any handshake (see `pairing::AbortReason`).
pub(crate) const PAIR_ABORT_UNKNOWN: u8 = 1;
pub(crate) const PAIR_ABORT_EXPIRED: u8 = 2;
pub(crate) const PAIR_VERSION: u8 = 1;

async fn pair_connection(inner: Arc<Inner>, conn: Connection) {
    let Ok(_slot) = inner.pair_slots.try_acquire() else {
        conn.close(VarInt::from_u32(CLOSE_BUSY), b"busy");
        return;
    };
    let Ok(Ok((mut send, mut recv))) = timeout(PAIR_HELLO_TIMEOUT, conn.accept_bi()).await else {
        conn.close(VarInt::from_u32(CLOSE_PAIRING), b"no hello");
        return;
    };
    // Saying nothing is not an attempt; anything that was said and is not the
    // right hello is.
    let named: Option<[u8; 8]> = match timeout(PAIR_HELLO_TIMEOUT, read_frame(&mut recv)).await {
        Err(_) => {
            conn.close(VarInt::from_u32(CLOSE_PAIRING), b"no hello");
            return;
        }
        Ok(Ok(Frame { kind: FrameType::PairHello, payload })) if payload.len() == 9 && payload[0] == PAIR_VERSION => payload[1..9].try_into().ok(),
        Ok(_) => None,
    };
    let now = (inner.cfg.clock)();
    let decision = {
        let mut slot = lock(&inner.invite);
        let matches = match (slot.as_ref(), named) {
            (Some(p), Some(id)) => muse_core::vault::secret::ct_eq(&p.id, &id),
            _ => false,
        };
        if matches {
            // Taken out of the table before anything else happens: single use.
            match slot.take() {
                Some(p) if now <= p.expires => Ok(p),
                _ => Err(PAIR_ABORT_EXPIRED),
            }
        } else {
            if inner.cfg.pairing.burn_on_any_failed_attempt {
                slot.take(); // dropping the sender tells the waiting session
            }
            Err(PAIR_ABORT_UNKNOWN)
        }
    };
    inner.refresh_alpns();
    match decision {
        Ok(pending) => {
            // If the session is gone the connection is dropped, which closes it.
            let _ = pending.tx.send(PairingConn { conn, send, recv });
        }
        Err(code) => {
            let _ = write_frame(&mut send, FrameType::PairAbort, &[code]).await;
            let _ = send.finish();
            let _ = timeout(Duration::from_secs(2), conn.closed()).await;
            conn.close(VarInt::from_u32(CLOSE_PAIRING), b"refused");
        }
    }
}

// ---------------------------------------------------------------- sync channel

/// An opaque, ordered, bounded message channel between two paired devices,
/// for the sync package. Nothing is queued here: `send` waits for QUIC flow
/// control, and a message is at most [`MAX_SYNC_MESSAGE`] bytes.
pub struct SyncChannel {
    name: String,
    send: SendStream,
    recv: RecvStream,
    closed: bool,
}

impl SyncChannel {
    pub fn name(&self) -> &str {
        &self.name
    }

    pub async fn send(&mut self, message: &[u8]) -> Result<(), LinkError> {
        if message.len() > MAX_SYNC_MESSAGE {
            return Err(LinkError::Frame(FrameError::TooLarge));
        }
        write_frame(&mut self.send, FrameType::SyncMessage, message).await.map_err(LinkError::from)
    }

    /// The next message; `None` once the other side closed the channel.
    pub async fn recv(&mut self) -> Result<Option<Vec<u8>>, LinkError> {
        if self.closed {
            return Ok(None);
        }
        match read_frame_or_end(&mut self.recv).await? {
            None | Some(Frame { kind: FrameType::SyncClose, .. }) => {
                self.closed = true;
                Ok(None)
            }
            Some(Frame { kind: FrameType::SyncMessage, payload }) => Ok(Some(payload)),
            Some(_) => Err(LinkError::Frame(FrameError::Malformed("not a sync frame"))),
        }
    }

    pub async fn close(mut self) -> Result<(), LinkError> {
        write_frame(&mut self.send, FrameType::SyncClose, &[]).await?;
        let _ = self.send.finish();
        Ok(())
    }
}

// ---------------------------------------------------------------- client

struct ClientState {
    conn: Option<Connection>,
    failures: u32,
    not_before: Option<Instant>,
}

/// Calls another device. Keeps one connection, reconnects when it is lost,
/// and waits longer between attempts each time one fails (so a device that is
/// off, or that has removed this one, is not hammered).
pub struct LinkClient {
    ep: Endpoint,
    addr: EndpointAddr,
    cfg: LinkConfig,
    state: tokio::sync::Mutex<ClientState>,
}

fn close_code(conn: &Connection) -> Option<u64> {
    match conn.close_reason() {
        Some(ConnectionError::ApplicationClosed(close)) => Some(close.error_code.into_inner()),
        _ => None,
    }
}

fn closed_error(conn: &Connection, fallback: String) -> LinkError {
    match close_code(conn) {
        Some(code) if code == CLOSE_NOT_PAIRED as u64 || code == CLOSE_REMOVED as u64 => LinkError::NotPaired,
        Some(code) if code == CLOSE_BUSY as u64 => LinkError::Busy,
        _ => LinkError::Closed(fallback),
    }
}

impl LinkClient {
    fn backoff(&self, failures: u32) -> Duration {
        if failures == 0 {
            return Duration::ZERO;
        }
        let doubled = self.cfg.backoff_min.saturating_mul(1u32 << (failures - 1).min(16));
        let base = doubled.min(self.cfg.backoff_max);
        // Up to a quarter more, so many clients do not retry in step. Not a secret.
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        base + base.mul_f64(f64::from(nanos % 1000) / 4000.0)
    }

    /// The delay before the next connection attempt would start (zero when none is owed).
    pub async fn next_attempt_in(&self) -> Duration {
        let state = self.state.lock().await;
        state.not_before.map(|t| t.saturating_duration_since(Instant::now())).unwrap_or(Duration::ZERO)
    }

    async fn connection(&self) -> Result<Connection, LinkError> {
        let mut state = self.state.lock().await;
        if let Some(conn) = &state.conn {
            if conn.close_reason().is_none() {
                return Ok(conn.clone());
            }
            state.conn = None;
        }
        let mut last = LinkError::Unreachable("no attempt made".into());
        for _ in 0..self.cfg.connect_attempts.max(1) {
            if let Some(at) = state.not_before {
                tokio::time::sleep_until(at).await;
            }
            // Every attempt counts as a failure until a request succeeds: a
            // device that removed this one completes the handshake and closes.
            state.failures = state.failures.saturating_add(1);
            state.not_before = Some(Instant::now() + self.backoff(state.failures));
            match timeout(self.cfg.connect_timeout, self.ep.connect(self.addr.clone(), LINK_ALPN)).await {
                Ok(Ok(conn)) => {
                    state.conn = Some(conn.clone());
                    return Ok(conn);
                }
                Ok(Err(e)) => last = LinkError::Unreachable(e.to_string()),
                Err(_) => last = LinkError::Unreachable("timed out".into()),
            }
        }
        Err(last)
    }

    async fn succeeded(&self) {
        let mut state = self.state.lock().await;
        state.failures = 0;
        state.not_before = None;
    }

    async fn open(&self) -> Result<(Connection, SendStream, RecvStream), LinkError> {
        let conn = self.connection().await?;
        match conn.open_bi().await {
            Ok((send, recv)) => Ok((conn, send, recv)),
            Err(first) => {
                if matches!(closed_error(&conn, String::new()), LinkError::NotPaired | LinkError::Busy) {
                    return Err(closed_error(&conn, first.to_string()));
                }
                // The kept connection had died and nothing was sent: once more on a new one.
                let conn = self.connection().await?;
                match conn.open_bi().await {
                    Ok((send, recv)) => Ok((conn, send, recv)),
                    Err(e) => Err(closed_error(&conn, e.to_string())),
                }
            }
        }
    }

    /// One home API call, exactly as over the local socket: method, path and
    /// body in; status and body out. A request is never sent twice.
    pub async fn request(&self, method: &str, path: &str, body: &[u8]) -> Result<Response, LinkError> {
        let payload = Request::new(method, path, body).encode()?;
        let (conn, mut send, mut recv) = self.open().await?;
        let exchange = async {
            write_frame(&mut send, FrameType::Request, &payload).await?;
            let _ = send.finish();
            read_frame(&mut recv).await
        };
        let frame = match timeout(self.cfg.response_timeout, exchange).await {
            Err(_) => {
                let _ = recv.stop(VarInt::from_u32(0));
                return Err(LinkError::Timeout);
            }
            Ok(Err(e)) => {
                // Give the close frame a moment to arrive, so the reason is the real one.
                let _ = timeout(Duration::from_millis(500), conn.closed()).await;
                return Err(match closed_error(&conn, e.to_string()) {
                    LinkError::Closed(_) if conn.close_reason().is_none() => LinkError::Frame(e),
                    other => other,
                });
            }
            Ok(Ok(frame)) => frame,
        };
        self.succeeded().await;
        match frame.kind {
            FrameType::Response => Ok(Response::decode(&frame.payload)?),
            FrameType::Refusal => Err(LinkError::Refused(Refusal::decode(&frame.payload)?)),
            _ => Err(LinkError::Frame(FrameError::Malformed("not a response"))),
        }
    }

    /// Opens a sync channel by name.
    pub async fn open_sync(&self, name: &str) -> Result<SyncChannel, LinkError> {
        if name.is_empty() || name.len() > MAX_SYNC_NAME {
            return Err(LinkError::Frame(FrameError::Malformed("sync channel name")));
        }
        let mut payload = Vec::new();
        frame::put_lp8(&mut payload, name.as_bytes());
        let (conn, mut send, mut recv) = self.open().await?;
        let exchange = async {
            write_frame(&mut send, FrameType::SyncOpen, &payload).await?;
            read_frame(&mut recv).await
        };
        match timeout(self.cfg.response_timeout, exchange).await {
            Err(_) => Err(LinkError::Timeout),
            Ok(Err(e)) => {
                let _ = timeout(Duration::from_millis(500), conn.closed()).await;
                Err(closed_error(&conn, e.to_string()))
            }
            Ok(Ok(Frame { kind: FrameType::SyncAccept, .. })) => {
                self.succeeded().await;
                Ok(SyncChannel { name: name.to_string(), send, recv, closed: false })
            }
            Ok(Ok(Frame { kind: FrameType::Refusal, payload })) => Err(LinkError::Refused(Refusal::decode(&payload)?)),
            Ok(Ok(_)) => Err(LinkError::Frame(FrameError::Malformed("not a sync accept"))),
        }
    }

    /// Closes the kept connection, if any.
    pub async fn close(&self) {
        if let Some(conn) = self.state.lock().await.conn.take() {
            conn.close(VarInt::from_u32(CLOSE_NORMAL), b"bye");
        }
    }

    /// For tests of the serving side: the raw connection, to send bytes the client never would.
    #[cfg(feature = "test-util")]
    pub async fn raw_connection(&self) -> Result<Connection, LinkError> {
        self.connection().await
    }
}
