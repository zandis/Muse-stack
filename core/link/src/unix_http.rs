//! A [`LinkHandler`] that forwards each call to an HTTP server on a Unix
//! socket: the home control API as it listens today (`MUSE_HOME/home.sock`).
//!
//! The request bytes are built, and the reply parsed, by the app's own
//! `muse-home-link` crate (the same strict subset of HTTP/1.1 the desktop app
//! speaks to the home), so there is one implementation of that. Only the
//! socket I/O is here, because that crate's is blocking.
//!
//! **Who is calling.** The home authenticates a device by its device token
//! (`Authorization: Bearer`, architecture section 4.1). Over the Link the
//! device is already authenticated by its key, so this bridge does not accept
//! a token from the peer and does not invent another kind: it looks up the
//! home's own token for that device ([`TokenSource`]) and adds it on the home
//! side. The token never crosses the Link. When the home's `verify(token) ->
//! Device` gains a `verify(peer) -> Device` twin, this lookup goes away.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use muse_home_link::{build_request, parse_response, MAX_REPLY};
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

use crate::frame::{Request, Response};
use crate::handler::{BoxFuture, LinkHandler, Peer};
use crate::registry::{DeviceId, Role};

/// The home's device token for a paired device, kept on the home.
pub trait TokenSource: Send + Sync + 'static {
    fn token_for(&self, peer: &Peer) -> Option<String>;
}

/// One home token per role: every paired `owner` device uses the home's
/// owner token, and so on. The simplest mapping of paired devices onto the
/// home's v1 roles.
#[derive(Default, Clone)]
pub struct RoleTokens {
    pub owner: Option<String>,
    pub host: Option<String>,
    pub viewer: Option<String>,
}

impl TokenSource for RoleTokens {
    fn token_for(&self, peer: &Peer) -> Option<String> {
        match peer.role {
            Role::Owner => self.owner.clone(),
            Role::Host => self.host.clone(),
            Role::Viewer => self.viewer.clone(),
        }
    }
}

/// One home token per device (made with `muse home token add` when the device was paired).
#[derive(Default, Clone)]
pub struct DeviceTokens(pub HashMap<DeviceId, String>);

impl TokenSource for DeviceTokens {
    fn token_for(&self, peer: &Peer) -> Option<String> {
        self.0.get(&peer.device_id).cloned()
    }
}

pub struct UnixHttpHandler {
    socket: PathBuf,
    tokens: Arc<dyn TokenSource>,
    timeout: Duration,
}

impl UnixHttpHandler {
    pub fn new(socket: impl Into<PathBuf>, tokens: Arc<dyn TokenSource>) -> UnixHttpHandler {
        UnixHttpHandler { socket: socket.into(), tokens, timeout: Duration::from_secs(15) }
    }

    pub fn with_timeout(mut self, timeout: Duration) -> UnixHttpHandler {
        self.timeout = timeout;
        self
    }

    async fn forward(&self, bytes: Vec<u8>) -> Result<Vec<u8>, &'static str> {
        let mut stream = UnixStream::connect(&self.socket).await.map_err(|_| "unreachable")?;
        stream.write_all(&bytes).await.map_err(|_| "unreachable")?;
        let mut raw = Vec::new();
        // One byte past the limit shows the reply was too large without reading more.
        let mut limited = (&mut stream).take(MAX_REPLY as u64 + 1);
        limited.read_to_end(&mut raw).await.map_err(|_| "unreachable")?;
        if raw.len() > MAX_REPLY {
            return Err("bad_reply");
        }
        Ok(raw)
    }
}

fn error(status: u16, code: &str) -> Response {
    Response::new(status, format!("{{\"error\":{{\"code\":\"{code}\"}}}}").as_bytes())
}

impl LinkHandler for UnixHttpHandler {
    fn handle<'a>(&'a self, peer: &'a Peer, request: Request) -> BoxFuture<'a, Response> {
        Box::pin(async move {
            let Some(token) = self.tokens.token_for(peer) else {
                return error(403, "no_token_for_device");
            };
            let body: Option<Value> = if request.body.is_empty() {
                None
            } else {
                match serde_json::from_slice(&request.body) {
                    Ok(value) => Some(value),
                    Err(_) => return error(400, "invalid_request"),
                }
            };
            // Refuses anything the home's API does not define (method, path
            // characters, body shape), so nothing can be smuggled into the headers.
            let bytes = match build_request(&request.method, &request.path, body.as_ref(), &token, "localhost") {
                Ok(bytes) => bytes,
                Err(e) => return error(400, e.code()),
            };
            let raw = match tokio::time::timeout(self.timeout, self.forward(bytes)).await {
                Err(_) => return error(504, "unreachable"),
                Ok(Err(code)) => return error(502, code),
                Ok(Ok(raw)) => raw,
            };
            match parse_response(&raw) {
                Ok(reply) => {
                    let body = if reply.body.is_null() { Vec::new() } else { serde_json::to_vec(&reply.body).unwrap_or_default() };
                    Response { status: reply.status, body }
                }
                Err(e) => error(502, e.code()),
            }
        })
    }
}
